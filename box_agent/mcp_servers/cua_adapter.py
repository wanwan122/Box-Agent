"""Native macOS App adapter over the official cua-driver MCP.

Ported from mac-cua ``data/guest/cua_adapter.py`` (dev/codex_v1.0@fe6f3af),
which was validated against cua-driver 0.25.0. The window-resolution, sheet,
focus, paste and session-recovery contracts follow the 0.25.0 foreground baseline.
Box-Agent adds only schema-negotiated screenshot sizing for model coordinates.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

PROTOCOL = '2024-11-05'
DRIVER_TIMEOUT_SECONDS = 60
APP_LAUNCH_TIMEOUT_SECONDS = 60
APP_LAUNCH_WINDOW_TIMEOUT_SECONDS = 30
APP_LAUNCH_POLL_SECONDS = 0.25
APP_LAUNCH_STABLE_SAMPLES = 2
CF_NUMBER_SINT64_TYPE = 4
CG_WINDOW_LIST_EXCLUDE_DESKTOP_ELEMENTS = 1 << 4
CG_NULL_WINDOW_ID = 0
# Box-Agent re-encodes images above this long edge before the model sees
# them. Ask the driver for at most this size so model pixels == driver pixels.
MAX_IMAGE_DIMENSION = 1568


class McpClient:
    """One serial MCP connection. A timeout poisons it: never replay mutations."""

    def __init__(self, command: list[str], timeout: float = DRIVER_TIMEOUT_SECONDS, *, env=None):
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.serial = 0
        self.inbox = queue.Queue()
        env = {k: v for k, v in (os.environ if env is None else env).items()
               if k not in {'OPENAI_API_KEY', 'OPENAI_BASE_URL'}}
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=sys.stderr, text=True, env=env)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                         'clientInfo': {'name': 'mac-cua-adapter', 'version': '1'}})
            self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            self.tools = {}
            cursor = None
            seen = set()
            while True:
                page = self.request('tools/list', {'cursor': cursor} if cursor else {})
                self.tools.update({tool['name']: tool for tool in page['tools']})
                cursor = page.get('nextCursor')
                if not cursor:
                    break
                if cursor in seen:
                    raise RuntimeError('Driver repeated tools/list cursor')
                seen.add(cursor)
            self.deadline = None
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.inbox.put(json.loads(line))
        except (ValueError, OSError) as error:
            self.inbox.put(error)
        finally:
            self.inbox.put(EOFError('Driver MCP connection closed'))

    def send(self, value: dict):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.serial += 1
        request_id = self.serial
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        deadline = min(time.monotonic() + self.timeout, self.deadline or float("inf"))
        while True:
            try:
                value = self.inbox.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                self.close()
                raise TimeoutError('Driver timed out; outcome unknown. Connection closed; do not replay.')
            if isinstance(value, Exception):
                self.close()
                raise RuntimeError('Driver connection failed; outcome unknown') from value
            if 'method' in value:
                if 'id' in value:
                    reply = {'result': {}} if value['method'] == 'ping' else {
                        'error': {'code': -32601, 'message': 'Client method not supported'}}
                    self.send({'jsonrpc': '2.0', 'id': value['id'], **reply})
                continue
            if value.get('id') != request_id:
                continue
            if 'error' in value:
                raise RuntimeError('Driver RPC error: ' + json.dumps(value['error']))
            return value['result']

    def call(self, name: str, args: dict) -> dict:
        return self.request('tools/call', {'name': name, 'arguments': args})

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


class ActionError(Exception):
    def __init__(self, result):
        self.result = result


def data(result: dict) -> dict:
    return result.get('structuredContent') or {}


def with_text_labels(elements: list[dict], result: dict) -> list[dict]:
    """Fill unlabeled addressable nodes from their display-only AXStaticText children."""
    labels = {}
    for content in result.get('content', []):
        if content.get('type') != 'text':
            continue
        parents = []
        for line in content.get('text', '').splitlines():
            indent = len(line) - len(line.lstrip())
            while parents and parents[-1][0] >= indent:
                parents.pop()
            indexed = re.match(r'\s*- \[(\d+)\] ', line)
            if indexed:
                parents.append((indent, int(indexed.group(1))))
                continue
            static = re.match(r'\s*- AXStaticText = ("(?:[^"\\]|\\.)*")', line)
            if static and parents:
                try:
                    labels.setdefault(parents[-1][1], json.loads(static.group(1)))
                except json.JSONDecodeError:
                    pass
    return [{**element, 'label': labels[element['element_index']]}
            if not element.get('label') and element.get('element_index') in labels else element
            for element in elements]


def compact_rows(elements: list[dict]) -> list[dict]:
    """Drop unlabeled single-cell row wrappers while preserving useful state."""
    compact = []
    position = 0
    while position < len(elements):
        row = elements[position]
        depth = row.get('depth', 0)
        end = position + 1
        while end < len(elements) and elements[end].get('depth', 0) > depth:
            end += 1
        children = [item for item in elements[position + 1:end]
                    if item.get('depth', 0) == depth + 1]
        if (row.get('role') == 'AXRow' and not row.get('label') and len(children) == 1
                and children[0].get('role') == 'AXCell' and children[0].get('element_token')):
            for original in elements[position + 1:end]:
                item = {**original, 'depth': original.get('depth', 0) - 1}
                if original is children[0]:
                    item.update({flag: True for flag in ('selected', 'focused')
                                 if row.get(flag) is True})
                compact.append(item)
            position = end
            continue
        compact.append(row)
        position += 1
    return compact


def failed(result: dict) -> bool:
    state = data(result)
    return bool(result.get('isError') or result.get('is_error') or state.get('is_error') or state.get('refusal')
                or state.get('effect') in {'refused', 'failed', 'delivery_failed', 'partial', 'suspected_noop'}
                or state.get('status') in {'refused', 'failed'})


def ended_session(name: str, text: str) -> dict | None:
    """Return start_session args when text proves the call was rejected unrun."""
    # 0.25 daemon wording.
    match = re.fullmatch(
        r"session '([^'\n]+)' has ended; tool call '" + re.escape(name)
        + r"' was rejected\. Call start_session with this id to revive it before "
          r"issuing further actions, or use a new session id\.", text)
    if match:
        return {'session': match[1]}
    return None


def rectangle(value):
    if not isinstance(value, dict):
        return None
    try:
        output = tuple(float(value[name]) for name in ('x', 'y')) + (
            float(value['width'] if 'width' in value else value['w']),
            float(value['height'] if 'height' in value else value['h']),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return output if all(math.isfinite(number) for number in output) else None


def clear_image(state):
    for name in ('image_frame', 'image_size', 'image_bounds', 'image_background_input'):
        state.pop(name, None)


class _CGPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [('width', ctypes.c_double), ('height', ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [('origin', _CGPoint), ('size', _CGSize)]


def native_window_inventory(pid: int) -> list[dict]:
    """Return same-process WindowServer records, including nonzero layers."""
    if sys.platform != 'darwin':
        return []
    core_foundation = ctypes.CDLL(
        '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    core_graphics = ctypes.CDLL(
        '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    core_graphics.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    core_graphics.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    core_graphics.CGRectMakeWithDictionaryRepresentation.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_CGRect)]
    core_graphics.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
    core_foundation.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    core_foundation.CFArrayGetCount.restype = ctypes.c_long
    core_foundation.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    core_foundation.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    core_foundation.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core_foundation.CFDictionaryGetValue.restype = ctypes.c_void_p
    core_foundation.CFNumberGetValue.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    core_foundation.CFNumberGetValue.restype = ctypes.c_bool
    core_foundation.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    core_foundation.CFBooleanGetValue.restype = ctypes.c_bool
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]

    keys = {name: ctypes.c_void_p.in_dll(core_graphics, name).value for name in (
        'kCGWindowNumber', 'kCGWindowOwnerPID', 'kCGWindowLayer',
        'kCGWindowBounds', 'kCGWindowIsOnscreen')}
    array = core_graphics.CGWindowListCopyWindowInfo(
        CG_WINDOW_LIST_EXCLUDE_DESKTOP_ELEMENTS, CG_NULL_WINDOW_ID)
    if not array:
        raise OSError('CGWindowListCopyWindowInfo returned no inventory')

    def value(record, name):
        return core_foundation.CFDictionaryGetValue(record, keys[name])

    def number(record, name):
        output = ctypes.c_longlong()
        raw = value(record, name)
        return (output.value if raw and core_foundation.CFNumberGetValue(
            raw, CF_NUMBER_SINT64_TYPE, ctypes.byref(output)) else None)

    try:
        total = core_foundation.CFArrayGetCount(array)
        rows = []
        for index in range(total):
            record = core_foundation.CFArrayGetValueAtIndex(array, index)
            owner = number(record, 'kCGWindowOwnerPID')
            window_id = number(record, 'kCGWindowNumber')
            layer = number(record, 'kCGWindowLayer')
            bounds = _CGRect()
            raw_bounds = value(record, 'kCGWindowBounds')
            if (owner != pid or window_id is None or layer is None or not raw_bounds
                    or not core_graphics.CGRectMakeWithDictionaryRepresentation(
                        raw_bounds, ctypes.byref(bounds))):
                continue
            raw_on_screen = value(record, 'kCGWindowIsOnscreen')
            rows.append({
                'pid': owner,
                'window_id': window_id,
                'title': '',
                'bounds': {'x': bounds.origin.x, 'y': bounds.origin.y,
                           'width': bounds.size.width, 'height': bounds.size.height},
                'layer': layer,
                # CGWindowList is front-to-back; match cua-driver's larger-is-frontmost value.
                'z_index': total - index,
                'is_on_screen': bool(raw_on_screen and
                                     core_foundation.CFBooleanGetValue(raw_on_screen)),
                'native_inventory': True,
            })
        return rows
    finally:
        core_foundation.CFRelease(array)


def app_elements(elements: list[dict]) -> list[dict]:
    """Keep app UI and app menus; omit the global Apple menu subtree."""
    visible = []
    position = 0
    while position < len(elements):
        element = elements[position]
        position += 1
        if element.get('role') == 'AXMenuBarItem':
            end = position
            while end < len(elements) and elements[end].get('depth', 0) > element.get('depth', 0):
                end += 1
            if element.get('label') in {'Apple', '\uf8ff'}:
                position = end
                continue
            # Unknown menu state stays expanded. Never use labels to infer visibility.
            if element.get('selected') is False and not any(
                    e.get(flag) is True for e in elements[position - 1:end]
                    for flag in ('selected', 'expanded', 'focused')):
                position = end
        visible.append(element)
    return visible


def active_menu_elements(elements: list[dict]) -> list[dict]:
    """Return the unique selected menu-bar item subtree, rebased as the active root."""
    selected = [(position, element) for position, element in enumerate(elements)
                if (element.get('role') == 'AXMenuBarItem'
                    and element.get('label') not in {'Apple', '\uf8ff'}
                    and element.get('selected') is True)]
    if len(selected) != 1:
        return []
    start, root = selected[0]
    depth = root.get('depth', 0)
    end = start + 1
    while end < len(elements) and elements[end].get('depth', 0) > depth:
        end += 1
    return [{**item, 'depth': max(0, item.get('depth', 0) - depth)}
            for item in elements[start:end]]


def attached_sheet_elements(elements: list[dict], bounds: dict | None = None) -> list[dict]:
    """Return the first AXSheet subtree, optionally requiring matching bounds."""
    target = rectangle(bounds) if bounds is not None else None
    if bounds is not None and target is None:
        return []
    for start, element in enumerate(elements):
        if str(element.get('role', '')).removeprefix('AX').lower() != 'sheet':
            continue
        frame = rectangle(element.get('frame'))
        # ponytail: frame equality is the only parent signal in cua-driver 0.25.0;
        # replace it when the driver exposes an attached-sheet parent window ID.
        if target is not None and frame != target:
            continue
        depth = element.get('depth', 0)
        end = start + 1
        while end < len(elements) and elements[end].get('depth', 0) > depth:
            end += 1
        return [{**item, 'depth': max(0, item.get('depth', 0) - depth)}
                for item in elements[start:end]]
    return []


def ax_line(index: int, element: dict) -> str:
    """One compact, single-line model-visible node; no driver snapshot identity."""
    readable = lambda name: re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name.removeprefix('AX')).lower()
    role = readable(element.get('role', 'element'))
    flags = [name for name in ('selected', 'focused', 'settable') if element.get(name) is True]
    if element.get('enabled') is False:
        flags.insert(0, 'disabled')
    if type(element.get('expanded')) is bool:
        flags.append('expanded' if element['expanded'] else 'collapsed')
    line = f'{index} {role}' + (f' ({", ".join(flags)})' if flags else '')
    label = element.get('label')
    if label and not str(label).startswith('_NS:'):
        line += ' ' + json.dumps(label, ensure_ascii=False)
    for key in ('value', 'description', 'help', 'identifier', 'url', 'min', 'max'):
        if key in element and element[key] is not None:
            line += f', {key.title()}: ' + json.dumps(element[key], ensure_ascii=False)
    actions = [readable(action) for action in element.get('actions', [])
               if action not in {'AXPress', 'AXPick', 'AXCancel'}]
    if actions:
        line += ', Actions: ' + ', '.join(actions)
    return '\t' * min(50, max(0, element.get('depth', 0))) + line


class Adapter:
    """Internal native API. Model IDs are positions in the latest app snapshot."""

    def __init__(self, driver, evidence=None):
        self.driver = driver
        # A cua-driver session belongs to this transport, not the REPL service.
        self.session = 'repl-' + uuid.uuid4().hex
        self.handles = {}
        self.started = False
        self.cell = None
        self.invocation_id = None
        self.evidence = evidence
        self.deadline = None
        self.record_counts = {}

    def validate(self, name, args):
        tool = self.driver.tools.get(name)
        if not tool:
            raise ValueError(f'UnsupportedCapability: driver does not expose {name}')
        schema = tool['inputSchema']
        props = schema.get('properties', {})
        args = dict(args)
        if 'session' in props:
            if name in {'start_session', 'end_session'}:
                args.setdefault('session', self.session)
            else:
                args['session'] = self.session
        if args.keys() - props.keys() or set(schema.get('required', [])) - args.keys():
            raise ValueError(f'UnsupportedCapability: incompatible driver schema for {name}')
        for key, value in args.items():
            field = props[key]
            if 'enum' in field and value not in field['enum']:
                raise ValueError(f'UnsupportedCapability: {name}.{key}={value!r}')
        return args

    def call(self, name, args, *, recover_session=True):
        args = self.validate(name, args)
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('JS deadline exceeded; outcome unknown')
            self.driver.timeout = min(DRIVER_TIMEOUT_SECONDS, remaining)
            self.driver.deadline = self.deadline
        entry = {'tool': name, 'arguments': args}
        self.record({'phase': 'dispatch', **entry})
        try:
            result = self.driver.call(name, args)
            entry['result'] = result
            self.record({'phase': 'result', **entry})
        except Exception as error:
            entry['error'] = str(error)
            self.record({'phase': 'error', **entry})
            raise
        if failed(result):
            # Only this exact daemon rejection proves the action never ran.
            # Restore the reported identity: sessionless tools use the MCP id.
            if recover_session and name not in {'start_session', 'end_session'}:
                for content in result.get('content', []):
                    revive = ended_session(name, content.get('text', ''))
                    if revive is not None:
                        self.call('start_session', revive, recover_session=False)
                        return self.call(name, args, recover_session=False)
            raise ActionError(result)
        return result

    def record(self, value):
        if self.evidence:
            self.evidence.write(json.dumps({'invocation_id': self.invocation_id, 'cell': self.cell,
                                            'driver_session_id': self.session, 'backend': 'native', **value}, ensure_ascii=False) + '\n')
            self.evidence.flush()
            counts = self.record_counts.setdefault(self.cell, {'dispatches': 0, 'errors': 0})
            if value.get('phase') == 'dispatch':
                counts['dispatches'] += 1
            elif value.get('phase') == 'error':
                counts['errors'] += 1

    def invalidate(self):
        for state in self.handles.values():
            state['fresh'] = False

    def installed_app(self, requested):
        apps = data(self.call('list_apps', {})).get('apps', [])
        if requested.startswith('/'):
            matches = [app for app in apps if app.get('launch_path') == requested]
        else:
            matches = [app for app in apps if app.get('bundle_id') == requested]
            if not matches:
                matches = [app for app in apps if app.get('name') == requested]
        if not matches:
            raise ValueError(f'ApplicationNotFound: {requested}')
        if len(matches) != 1:
            raise ValueError(f'ApplicationAmbiguous: {requested}')
        return matches[0]

    def stable_launched_app(self, bundle_id, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + APP_LAUNCH_TIMEOUT_SECONDS
        previous_pid = None
        stable_samples = 0
        while True:
            apps = data(self.call('list_apps', {})).get('apps', [])
            running = [app for app in apps if app.get('bundle_id') == bundle_id
                       and app.get('running') is True
                       and type(app.get('pid')) is int and app['pid'] > 0]
            if len(running) > 1:
                raise ValueError(f'ApplicationAmbiguous: {bundle_id}')
            pid = running[0]['pid'] if running else None
            if pid is not None and pid == previous_pid:
                stable_samples += 1
            else:
                previous_pid = pid
                stable_samples = 1 if pid is not None else 0
            if stable_samples >= APP_LAUNCH_STABLE_SAMPLES:
                return running[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(
                    f'ApplicationLaunchUnstable: {bundle_id} did not keep a stable PID')
            time.sleep(min(APP_LAUNCH_POLL_SECONDS, remaining))

    def wait_for_launch_window(self, state):
        deadline = time.monotonic() + APP_LAUNCH_WINDOW_TIMEOUT_SECONDS
        while True:
            try:
                if self.visible_windows(state):
                    return True
            except ValueError as error:
                if str(error) != 'ApplicationChanged: process identity expired; call cua.getApp again':
                    raise
                opened = self.stable_launched_app(state['bundle_id'], deadline)
                state['pid'] = opened['pid']
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(APP_LAUNCH_POLL_SECONDS, remaining))

    def visible_windows(self, state):
        if state.get('bundle_id'):
            apps = data(self.call('list_apps', {})).get('apps', [])
            live = [app for app in apps if app.get('bundle_id') == state['bundle_id']
                    and app.get('pid') == state['pid'] and app.get('running') is not False]
            if len(live) != 1:
                raise ValueError('ApplicationChanged: process identity expired; call cua.getApp again')
            state['app_active'] = live[0].get('active') is True
        result = self.call('list_windows', {'pid': state['pid']})
        rows = [w for w in data(result).get('windows', []) if
                w.get('pid', state['pid']) == state['pid'] and w.get('is_on_screen') is True]
        return rows

    @staticmethod
    def native_window_key(window):
        bounds = window.get('bounds') or {}
        return (window['window_id'], tuple(bounds.get(name) for name in
                                           ('x', 'y', 'width', 'height')))

    def verify_native_window(self, state, window):
        """Ask the driver whether an exact WindowServer ID is a top-level AX window."""
        try:
            result = self.call('get_window_state', {
                'pid': state['pid'], 'window_id': window['window_id'],
                'include_screenshot': False, 'include_accessibility_tree': True})
        except ActionError:
            return False
        raw = data(result)
        roots = [element for element in raw.get('elements', [])
                 if element.get('depth', 0) == 0 and element.get('role') == 'AXWindow']
        return (raw.get('pid') == state['pid']
                and raw.get('window_id') == window['window_id']
                and len(roots) == 1)

    def interaction_windows(self, state, verify_native=False):
        """Add same-process top-level windows omitted by driver list_windows."""
        rows = self.visible_windows(state)
        try:
            native = native_window_inventory(state['pid'])
        except OSError as error:
            state['native_inventory_error'] = str(error)
            return rows
        state.pop('native_inventory_error', None)
        known = {window['window_id'] for window in rows}
        extra = [window for window in native
                 if window.get('pid') == state['pid']
                 and window['window_id'] not in known
                 and window.get('is_on_screen') is True
                 and window.get('bounds', {}).get('width', 0) > 0
                 and window.get('bounds', {}).get('height', 0) > 0]
        cache = state.setdefault('native_window_validation', {})
        live = {self.native_window_key(window) for window in extra}
        for key in cache.keys() - live:
            del cache[key]
        if verify_native:
            for window in sorted(extra, key=lambda item: item.get('z_index', -1), reverse=True):
                key = self.native_window_key(window)
                if cache.get(key) is not True:
                    self.invalidate()
                    state['fresh'] = False
                    cache[key] = self.verify_native_window(state, window)
        supplemental = []
        for window in extra:
            verified = cache.get(self.native_window_key(window))
            if verified is not False:
                supplemental.append({**window, 'native_verified': verified})
        return rows + supplemental

    def windows(self, state):
        return self.selected_window(state, self.interaction_windows(state, verify_native=True))

    def selected_window(self, state, rows):
        if any(w.get('is_focused') is True for w in rows):
            state.pop('focus_hint', None)
            return self.current_window(rows)
        hint = state.get('focus_hint')
        frames = [self.frame(w) for w in sorted(rows, key=lambda w: w['window_id'])]
        if hint and hint['frames'] == frames:
            matches = [w for w in rows if w['window_id'] == hint['window_id']]
            if len(matches) == 1:
                return matches[0]
        state.pop('focus_hint', None)
        return self.current_window(rows)

    @staticmethod
    def current_window(rows):
        ranked_verified = [w for w in rows
                           if not (w.get('native_inventory') is True
                                   and w.get('native_verified') is None)
                           and type(w.get('z_index')) is int]
        verified_top = max((w['z_index'] for w in ranked_verified), default=None)
        unknown = [w for w in rows if w.get('native_inventory') is True
                   and w.get('native_verified') is None
                   and type(w.get('z_index')) is int
                   and (verified_top is None or w['z_index'] >= verified_top)]
        if unknown:
            raise ValueError('WindowUnverified: a new native surface must be observed before input')
        native = [w for w in rows if w.get('native_inventory') is True]
        if native:
            ranked = [w for w in rows if type(w.get('z_index')) is int]
            top = max((w['z_index'] for w in ranked), default=None)
            winners = [w for w in native if w.get('z_index') == top]
            if len(winners) == 1:
                return winners[0]
            if winners:
                raise ValueError('WindowAmbiguous: multiple native windows are frontmost')
        focused = [w for w in rows if w.get('is_focused') is True]
        if len(focused) == 1:
            return focused[0]
        if len(rows) == 1:
            return rows[0]
        ranked = [w for w in rows if type(w.get('z_index')) is int]
        if len(ranked) == len(rows) and ranked:
            # Driver reverses CGWindowList order: the largest z_index is frontmost.
            top = max(w['z_index'] for w in ranked)
            winners = [w for w in ranked if w['z_index'] == top]
            if len(winners) == 1:
                return winners[0]
        raise ValueError('WindowAmbiguous: no proven current window. Inspect app/window UI; '
                         'cross-process panels cannot be guessed. Candidates: ' + json.dumps(rows))

    @staticmethod
    def frame(window):
        return (window['window_id'], window.get('bounds'))

    def focus_window(self, state, active_window, allow_correction=False):
        if (active_window.get('native_inventory') is True
                and active_window.get('native_verified') is True):
            if state.get('app_active') is not True:
                raise ValueError('Window focus was not verified; input not sent. Observe again.')
            return active_window
        try:
            focus = self.call('bring_to_front', {'pid': state['pid'], 'window_id': active_window['window_id']})
        except ActionError as error:
            focus = error.result
        if data(focus).get('exact_window_effect', {}).get('verified') is not True:
            report = data(focus)
            observed = report.get('observed', {})
            wid = observed.get('focused_window_id')
            # One bounded correction before input; never reinterpret screenshot coordinates.
            if (not allow_correction
                    or report.get('code') != 'bring_to_front_exact_window_unverified'
                    or report.get('pid') != state['pid']
                    or report.get('window_id') != active_window['window_id']
                    or type(wid) is not int or wid == active_window['window_id']
                    or observed.get('front_process_matches_target') is not True
                    or observed.get('frontmost_pid') != state['pid']
                    or observed.get('workspace_frontmost_pid') != state['pid']
                    or observed.get('frontmost_ordinary_window_id') != wid):
                if failed(focus):
                    raise ActionError(focus)
                raise ValueError('Window focus was not verified; input not sent. Observe again.')
            rows = self.interaction_windows(state)
            matches = [w for w in rows if w['window_id'] == wid]
            if len(matches) != 1:
                raise ValueError('WindowChanged: reported focus is not a visible app window; input not sent')
            corrected = matches[0]
            focus = self.call('bring_to_front', {'pid': state['pid'], 'window_id': wid})
            if data(focus).get('exact_window_effect', {}).get('verified') is not True:
                raise ValueError('Window focus correction was not verified; input not sent')
            active_window = corrected
            state.update(window=corrected, focus_hint={
                'window_id': wid,
                'frames': [self.frame(w) for w in sorted(rows, key=lambda w: w['window_id'])]})
            clear_image(state)
        return active_window

    def verify_frame(self, state, window):
        try:
            rows = self.interaction_windows(state)
            current = self.selected_window(state, rows)
            if self.frame(current) != self.frame(window):
                raise ValueError('WindowChanged: observe the current target again')
            return rows
        except Exception:
            clear_image(state)
            self.invalidate()
            raise

    def observe(self, state, method, disable_diff=False):
        if method != 'getScreenshot':
            self.invalidate()
            state['fresh'] = False
            clear_image(state)
        rows = sorted(self.interaction_windows(state, verify_native=True), key=lambda w: w['window_id'])
        try:
            current = self.selected_window(state, rows)
        except ValueError:
            if method == 'getScreenshot':
                raise
            current = None
        if method != 'getAXState' and current is not None:
            clear_image(state)
            current = self.focus_window(state, current, allow_correction=True)
            rows = sorted(self.verify_frame(state, current), key=lambda w: w['window_id'])
        if method == 'getScreenshot':
            observation = self.observe_window(state, method, current)
            self.verify_frame(state, current)
            return observation
        snapshots = state.get('window_states', {})
        elements, targets = {}, {}
        image = None
        source = None
        rendered = ''
        updated = {}
        if current is not None:
            wid = current['window_id']
            child = dict(snapshots.get(wid, {}))
            child.update(pid=state['pid'], bundle_id=state.get('bundle_id'),
                         native_inventory_error=state.get('native_inventory_error'))
            observation = self.observe_window(
                child, 'getAXStateAndScreenshot' if method == 'getAXStateAndScreenshot' else 'getAXState',
                current, disable_diff, scope=('window', wid))
            image = observation['image']
            if image:
                image_geometry = {name: child[name] for name in (
                    'image_frame', 'image_size', 'image_bounds', 'image_background_input')
                    if name in child}
            if child['elements']:
                source, rendered, updated[wid] = current, observation['state'], child
            else:
                for candidate in sorted((w for w in rows if w['window_id'] != wid),
                                        key=lambda w: w.get('z_index', -1), reverse=True):
                    candidate_id = candidate['window_id']
                    candidate_state = dict(snapshots.get(candidate_id, {}))
                    candidate_state.update(pid=state['pid'], bundle_id=state.get('bundle_id'))
                    sheet = self.observe_window(
                        candidate_state, 'getAXState', candidate, disable_diff,
                        sheet_bounds=current.get('bounds'), display_window=current)
                    if candidate_state['elements']:
                        source, rendered = candidate, sheet['state']
                        updated[candidate_id] = candidate_state
                        break
        if source is not None:
            elements.update(updated[source['window_id']]['elements'])
            targets.update({index: source for index in elements})
        if image:
            self.verify_frame(state, current)
            clear_image(state)
            state.update(image_geometry)
        state.update(window=current, window_states=updated, elements=elements,
                     element_windows=targets, fresh=True)
        launch_window_timeout = state.pop('launch_window_timeout', None)
        if not elements:
            rendered = ((f'Application launched successfully, but no visible window appeared within '
                         f'{launch_window_timeout:g} seconds.')
                        if not rows and launch_window_timeout is not None else
                        'Application is running but has no visible windows.' if not rows else
                        'App AX unavailable: no accessible elements in the current window.')
        return {'state': rendered, 'image': image}

    def observe_window(self, state, method, window, disable_diff=False, offset=0,
                       sheet_bounds=None, display_window=None, scope=None):
        previous = state.get('window')
        if previous and self.frame(previous) != self.frame(window):
            clear_image(state)
        state['window'] = window
        identity = {'pid': state['pid'], 'window_id': window['window_id']}
        tree = method != 'getScreenshot'
        result = self.call('get_window_state', {**identity, 'include_screenshot': method != 'getAXState',
                                                'include_accessibility_tree': tree,
                                                **self.image_limit()})
        raw = data(result)
        if any(key in raw and raw[key] != value for key, value in identity.items()):
            self.invalidate()
            raise ValueError('WindowChanged: driver observation does not belong to the requested app/window')
        image = next((c for c in result.get('content', []) if c.get('type') == 'image'), None)
        if image:
            state['image_frame'] = self.frame(window)
            state['image_size'] = (raw.get('screenshot_width'), raw.get('screenshot_height'))
            bounds = rectangle(raw.get('window_bounds'))
            if raw.get('screenshot_frame_valid') is True and bounds:
                state['image_bounds'] = bounds
                state['image_background_input'] = raw.get('background_input')
            else:
                state.pop('image_bounds', None)
                state.pop('image_background_input', None)
        else:
            clear_image(state)
        if method == 'getScreenshot' and not image:
            raise ValueError('Screenshot unavailable; inspect state before choosing coordinates')
        output = {'image': image}
        if tree:
            # A new driver snapshot invalidates any sibling handle's element tokens.
            self.invalidate()
            raw_elements = compact_rows(with_text_labels(raw.get('elements', []), result))
            menu = active_menu_elements(raw_elements)
            sheet = (attached_sheet_elements(raw_elements, sheet_bounds)
                     if not menu and sheet_bounds is not None else [])
            visible = menu or (sheet if sheet or sheet_bounds is not None else app_elements(raw_elements))
            elements = dict(enumerate(visible, start=offset))
            lines = {index: ax_line(index, element) for index, element in elements.items()}
            shown = display_window or window
            title = ('Attached sheet: ' if sheet else 'Window: ')
            shown_title = (shown.get('title', '') if display_window else
                           raw.get('window_title', shown.get('title', '')))
            title += json.dumps(shown_title, ensure_ascii=False)
            title += ', App: ' + json.dumps(raw.get('app_name') or state.get('bundle_id') or state['pid'], ensure_ascii=False)
            notices = []
            if raw.get('degraded_reason') and sheet_bounds is None:
                notices.append('AX unavailable: ' + str(raw['degraded_reason']))
            if state.get('native_inventory_error'):
                notices.append('Native window inventory unavailable: ' + state['native_inventory_error'])
            title += ', Window ID: ' + str(shown['window_id'])
            if display_window and sheet:
                title += ', AX source window ID: ' + str(window['window_id'])
            root = (('menu', menu[0].get('label')) if menu else
                    ('sheet', shown['window_id'], self.frame(shown)) if sheet else scope)
            context = (title, notices, offset, root)
            same_context = state.get('tree_window') == window['window_id'] and state.get('tree_context') == context
            previous_lines = state.get('lines', {})
            if not disable_diff and same_context and lines == previous_lines:
                rendered = 'There has been no change in the accessibility tree for ' + title
            elif not disable_diff and same_context:
                changes = [('~ ' if k in previous_lines else '+ ') + v for k, v in lines.items()
                           if previous_lines.get(k) != v]
                removed = previous_lines.keys() - lines.keys()
                if removed:
                    changes.append('Removed element IDs: ' + ', '.join(map(str, sorted(removed))))
                rendered = title + '\nAX changes (IDs are positions in the latest snapshot):\n' + '\n'.join(changes)
                if not lines:
                    rendered += '\nNo accessible elements'
            else:
                rendered = title + '\n' + ('\n'.join(lines.values()) or 'No accessible elements')
            if notices:
                rendered += '\n' + '\n'.join(notices)
            # Refresh action tokens even when the rendered snapshot is unchanged.
            state.update(elements=elements, lines=lines, tree_window=window['window_id'],
                         tree_context=context, fresh=True)
            output['state'] = rendered
        return output

    @staticmethod
    def point(value):
        if not isinstance(value, list) or len(value) != 2 or any(
                type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in value):
            raise ValueError('Expected [x,y] with finite nonnegative screenshot coordinates')
        return value

    def target(self, state, target):
        if type(target) is int:
            element = state.get('elements', {}).get(target)
            if not state.get('fresh') or not element or not element.get('element_token'):
                raise ValueError('StaleElement: observe again and use an ID from that observation')
            return {'element_token': element['element_token']}
        x, y = self.point(target)
        if state.get('image_frame') != self.frame(state['window']):
            raise ValueError('Coordinates require a current screenshot of this window')
        width, height = state.get('image_size', (None, None))
        if (width is not None and x >= width) or (height is not None and y >= height):
            raise ValueError('Coordinates fall outside the observed screenshot')
        return {'x': x, 'y': y}

    def element_point(self, state, element):
        if state.get('image_frame') != self.frame(state['window']):
            raise ValueError('UnsupportedCapability: indexed pixel fallback requires a current screenshot')
        bounds = state.get('image_bounds')
        frame = rectangle(element.get('frame'))
        size = state.get('image_size')
        if (not bounds or not frame or not isinstance(size, tuple) or len(size) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (*size, bounds[2], bounds[3], frame[2], frame[3]))):
            raise ValueError('UnsupportedCapability: element lacks verified screenshot geometry')
        left, top = max(frame[0], bounds[0]), max(frame[1], bounds[1])
        right = min(frame[0] + frame[2], bounds[0] + bounds[2])
        bottom = min(frame[1] + frame[3], bounds[1] + bounds[3])
        if right <= left or bottom <= top:
            raise ValueError('UnsupportedCapability: element is outside the current screenshot')
        x = ((left + right) / 2 - bounds[0]) * size[0] / bounds[2]
        y = ((top + bottom) / 2 - bounds[1]) * size[1] / bounds[3]
        return {'x': round(x, 2), 'y': round(y, 2)}

    def pixel_click_route(self, state, window, params):
        if not (window.get('native_inventory') is True
                and window.get('native_verified') is True):
            return ({'pid': state['pid'], 'window_id': window['window_id']},
                    {**params, 'delivery_mode': 'foreground'})
        report = state.get('image_background_input') or {}
        exact = report.get('exact_window') or {}
        bounds = state.get('image_bounds')
        size = state.get('image_size')
        if (exact.get('pid') != state['pid'] or exact.get('window_id') != window['window_id']
                or exact.get('status') != 'matched'
                or not bounds or not isinstance(size, tuple) or len(size) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (*size, bounds[2], bounds[3]))):
            raise ValueError('UnsupportedCapability: supplemental window lacks exact screenshot geometry')
        screen_x = bounds[0] + params['x'] * bounds[2] / size[0]
        screen_y = bounds[1] + params['y'] * bounds[3] / size[1]
        return ({'scope': 'desktop'},
                {**params, 'x': round(screen_x, 2), 'y': round(screen_y, 2),
                 'delivery_mode': 'foreground'})

    def control(self, method, handle=None, args=None):
        args = args or {}
        if method in {'selectText', 'performSecondaryAction'}:
            raise ValueError(f'UnsupportedCapability: {method} requires a driver extension')
        if not self.started:
            self.call('start_session', {'session': self.session})
            self.started = True
        if method == 'listApps':
            rows = data(self.call('list_apps', {})).get('apps', [])
            return [{'id': a.get('bundle_id') or a.get('launch_path') or a.get('name'),
                     **({'displayName': a['name']} if a.get('name') else {}),
                     **({'isRunning': a['running']} if type(a.get('running')) is bool else {})} for a in rows]
        if method == 'getApp':
            app = args.get('app')
            if not isinstance(app, str) or not app.strip():
                raise ValueError('getApp requires a name, bundle ID or absolute .app path')
            installed = self.installed_app(app)
            bundle_id = installed.get('bundle_id')
            if not bundle_id:
                raise ValueError(f'ApplicationNotFound: {app} has no bundle ID')
            launched = installed.get('running') is not True
            if not launched:
                if type(installed.get('pid')) is not int or installed['pid'] <= 0:
                    raise ValueError(f'ApplicationUnavailable: {bundle_id} has no running PID')
                opened = installed
            else:
                self.call('launch_app', {'bundle_id': bundle_id})
                opened = self.stable_launched_app(bundle_id)
            state = {'pid': opened['pid'], 'bundle_id': bundle_id, 'fresh': False}
            if launched and not self.wait_for_launch_window(state):
                state['launch_window_timeout'] = APP_LAUNCH_WINDOW_TIMEOUT_SECONDS
            observation = self.observe(state, 'getAXState')
            handle = uuid.uuid4().hex
            self.handles[handle] = state
            return {'handle': handle, 'state': observation['state']}
        if handle not in self.handles:
            raise ValueError('Invalid app reference; acquire it again with cua.getApp')
        state = self.handles[handle]
        if method == 'listWindows':
            rows = self.interaction_windows(state, verify_native=True)
            active = self.selected_window(state, rows)
            return [{'id': w['window_id'], 'title': w.get('title', ''), 'bounds': w.get('bounds'),
                     'layer': w.get('layer', 0), 'isActive': self.frame(w) == self.frame(active),
                     **({'isFocused': w['is_focused']} if type(w.get('is_focused')) is bool else {})}
                    for w in sorted(rows, key=lambda w: w['window_id'])]
        if method in {'getAXState', 'getScreenshot', 'getAXStateAndScreenshot'}:
            return self.observe(state, method, args.get('disableDiffing', False))
        active_window = state.get('window')
        target_window = active_window
        indexed = method in {'click', 'scroll', 'setValue'} and type(args.get('target')) is int
        if indexed:
            self.target(state, args['target'])  # Validate freshness before selecting its window.
            target_window = state['element_windows'][args['target']]
        if active_window is None:
            raise ValueError('WindowAmbiguous: observe a current window before keyboard or coordinate input')
        identity = {'pid': state['pid'], 'window_id': target_window['window_id']}
        name, params = '', {}
        pixel_fallback = False
        if method == 'click':
            opts = args.get('options', {})
            if not isinstance(opts, dict) or opts.keys() - {'mouseButton', 'clickCount'}:
                raise ValueError('Unsupported click options')
            button = opts.get('mouseButton', 'left')
            button = {'l': 'left', 'r': 'right', 'm': 'middle'}.get(button, button)
            count = opts.get('clickCount', 1)
            if button not in {'left', 'right', 'middle'} or type(count) is not int or count < 1:
                raise ValueError('Invalid mouse button/click count')
            params = self.target(state, args.get('target'))
            if ('element_token' in params and count == 1 and button == 'left'
                    and 'AXPress' not in state['elements'][args['target']].get('actions', [])):
                element = state['elements'][args['target']]
                self.observe(state, 'getScreenshot')
                active_window = state['window']
                target_window = active_window
                identity['window_id'] = active_window['window_id']
                params = self.element_point(state, element)
                pixel_fallback = True
            if 'element_token' in params and count != 1:
                if count != 2 or button != 'left':
                    raise ValueError('UnsupportedCapability: indexed clicks support one click or left double click')
                name = 'double_click'
            else:
                name = 'click'
                params.update(button=button)
                if 'x' in params:
                    params['count'] = count
            if 'x' in params:
                identity, params = self.pixel_click_route(state, target_window, params)
            else:
                params['delivery_mode'] = 'foreground'
        elif method == 'drag':
            start = self.target(state, args.get('from'))
            end = self.target(state, args.get('to'))
            if 'x' not in end:
                raise ValueError('drag requires two coordinate pairs')
            if 'x' not in start:
                raise ValueError('drag requires two coordinate pairs')
            name = 'drag'
            params = dict(from_x=start['x'], from_y=start['y'], to_x=end['x'], to_y=end['y'], delivery_mode='foreground')
        elif method == 'scroll':
            direction = args.get('direction')
            direction = {'u': 'up', 'd': 'down', 'l': 'left', 'r': 'right'}.get(direction, direction)
            pages = args.get('pages', 1)
            if direction not in {'up', 'down', 'left', 'right'} or type(pages) is not int or not 1 <= pages <= 50:
                raise ValueError('scroll needs a direction and 1–50 integer pages (driver page steps)')
            name = 'scroll'
            params = {**self.target(state, args.get('target')), 'direction': direction, 'by': 'page',
                      'amount': pages, 'delivery_mode': 'foreground'}
        elif method == 'setValue':
            if type(args.get('target')) is not int or not isinstance(args.get('value'), str):
                raise ValueError('setValue requires a current AX element index and string value')
            name = 'set_value'
            params = {**self.target(state, args['target']), 'value': args['value']}
        elif method == 'typeText':
            if not isinstance(args.get('text'), str):
                raise ValueError('typeText requires a string')
            name, params = 'type_text', {'text': args['text'], 'delivery_mode': 'foreground'}
        elif method == 'paste':
            opts = args.get('options', {})
            if (not isinstance(args.get('text'), str) or not isinstance(opts, dict)
                    or opts.keys() - {'format'} or opts.get('format', 'text') != 'text'):
                raise ValueError('paste requires a string and optional {format: "text"}; rich text is unsupported')
            self.validate('clipboard_write', {'text': args['text']})
            name, identity, params = 'hotkey', {'scope': 'desktop'}, {'keys': ['cmd', 'v']}
        elif method == 'pressKey':
            key = args.get('key')
            if not isinstance(key, str) or not key or any(not k for k in key.split('+')):
                raise ValueError('pressKey requires a key string, e.g. super+s')
            aliases = {'super': 'cmd', 'control': 'ctrl', 'alt': 'option', 'enter': 'return',
                       'backspace': 'delete', 'esc': 'escape', 'prior': 'pageup', 'next': 'pagedown'}
            keys = [aliases.get(k.lower(), k.lower()) for k in key.split('+')]
            name = 'hotkey' if len(keys) > 1 else 'press_key'
            identity = {'scope': 'desktop'}
            params = {'keys': keys} if len(keys) > 1 else {'key': keys[0]}
        else:
            raise ValueError('Unknown native App method: ' + method)
        # All argument failures precede input delivery; indexed pixel fallback may take a screenshot.
        self.validate(name, {**identity, **params})
        def check_windows():
            rows = self.interaction_windows(state)
            active = self.selected_window(state, rows)
            source = [w for w in rows if w['window_id'] == target_window['window_id']]
            if (self.frame(active) != self.frame(active_window) or len(source) != 1
                    or self.frame(source[0]) != self.frame(target_window)):
                self.invalidate()
                clear_image(state)
                raise ValueError('WindowChanged: active or AX source window changed; observe again')

        check_windows()
        self.invalidate()
        # The sheet can be active while its elements come from the document's AX tree.
        # Focus the active surface; keep the original source window/token for the driver.
        corrected = self.focus_window(state, active_window,
                                      (indexed and not pixel_fallback)
                                      or method in {'pressKey', 'typeText', 'paste'})
        if corrected['window_id'] != active_window['window_id']:
            active_window = corrected
            if not indexed:
                target_window = corrected
                if method == 'typeText':
                    identity['window_id'] = corrected['window_id']
        check_windows()
        if method == 'paste':
            try:
                # Clipboard contents intentionally remain changed; arbitrary formats cannot be restored.
                written = self.call('clipboard_write', {'text': args['text']})
                if data(written).get('supported') is not True or data(written).get('written_type') != 'text':
                    raise ValueError('Clipboard text write was not confirmed')
                check_windows()
                self.focus_window(state, active_window)
                check_windows()
                result = self.call(name, {**identity, **params})
            except Exception as error:
                detail = str(error)
                message = 'Clipboard may have changed; paste is unconfirmed. Observe before continuing; do not replay. ' + detail
                if isinstance(error, (TimeoutError, EOFError, OSError, RuntimeError)):
                    raise type(error)(message) from error
                raise ValueError(message) from error
        else:
            result = self.call(name, {**identity, **params})
        effect = data(result).get('effect')
        if effect and effect != 'confirmed':
            return {'effect': effect}
        return {}

    def image_limit(self):
        props = self.driver.tools.get('get_window_state', {}).get('inputSchema', {}).get('properties', {})
        return {'max_image_dimension': MAX_IMAGE_DIMENSION} if 'max_image_dimension' in props else {}

    def close(self):
        self.deadline = None
        self.driver.deadline = None
        if self.started and 'end_session' in self.driver.tools:
            self.driver.timeout = 1
            self.call('end_session', {})
        self.handles.clear()
        self.started = False
