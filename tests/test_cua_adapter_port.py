"""Ported mac-cua adapter contract tests (fake driver; no GUI/model).

Adapter-level cases are copied from mac-cua tests/test_cua_adapter.py
(dev/codex_v1.0@fe6f3af) unchanged except for the import; the JS REPL cases
are re-expressed against the Box-Agent MCP tool layer in test_cua_server.py.
"""
import copy
import io
import json
import sys

import pytest

from box_agent.mcp_servers import cua_adapter as adapter

PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAD0lEQVR4nGP4jwMwDC0JALoev0Ewkwr8AAAAAElFTkSuQmCC'


def result(**state):
    return {'content': [], 'structuredContent': state}


class FakeDriver:
    timeout = 30
    def __init__(self):
        fields = {
            'start_session': 'session', 'end_session': 'session', 'list_apps': '',
            'launch_app': 'name bundle_id', 'list_windows': 'pid',
            'get_window_state': 'pid window_id include_screenshot include_accessibility_tree session',
            'click': 'scope pid window_id element_token x y button count action delivery_mode session',
            'double_click': 'pid window_id element_token delivery_mode session',
            'drag': 'pid window_id from_x from_y to_x to_y delivery_mode session',
            'scroll': 'scope pid window_id x y element_token direction by amount delivery_mode session',
            'type_text': 'pid window_id text delivery_mode session',
            'set_value': 'pid window_id element_token value session',
            'clipboard_write': 'text session',
            'hotkey': 'scope pid window_id keys delivery_mode session',
            'press_key': 'scope pid window_id key delivery_mode session',
            'bring_to_front': 'pid window_id',
        }
        self.tools = {name: {'name': name, 'inputSchema': {
            'type': 'object', 'properties': {key: {} for key in keys.split()}}} for name, keys in fields.items()}
        self.calls = []
        self.closed = False
        self.responses = {
            'start_session': result(active=True), 'launch_app': result(pid=42, bundle_id='com.apple.TextEdit'),
            'list_apps': result(apps=[{'bundle_id': 'com.apple.TextEdit', 'name': 'TextEdit',
                'launch_path': '/System/Applications/TextEdit.app', 'running': True, 'pid': 42}]),
            'list_windows': result(windows=[{'window_id': 7, 'pid': 42, 'title': 'Untitled',
                'bounds': {'x': 0, 'y': 0, 'width': 800, 'height': 600}, 'is_on_screen': True}]),
            'get_window_state': {**result(snapshot_id='s1', elements=[{'element_index': 1,
                'element_token': 's1:1', 'role': 'button', 'label': 'OK', 'depth': 0,
                'actions': ['AXPress']}]),
                'content': [{'type': 'image', 'mimeType': 'image/png', 'data': PNG}]},
            'bring_to_front': result(exact_window_effect={'verified': True}),
            'clipboard_write': result(supported=True, written_type='text'),
        }
    def call(self, name, args):
        self.calls.append((name, args))
        value = copy.deepcopy(self.responses.get(name, result(effect='unverifiable')))
        if name == 'get_window_state' and not args.get('include_screenshot'):
            value['content'] = [item for item in value['content'] if item.get('type') != 'image']
        return value
    def close(self):
        self.closed = True


def test_rendered_diff_renumbers_and_refreshes_targets():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    raw = driver.responses['get_window_state']['structuredContent']
    first = api.observe(state, 'getAXState')['state']
    assert '0 button "OK"' in first
    element = raw['elements'][0]
    element['element_token'] = 's2:1'
    assert 'no change' in api.observe(state, 'getAXState')['state']
    assert api.target(state, 0) == {'element_token': 's2:1'}
    element['selected'] = True
    assert '~ 0 button (selected)' in api.observe(state, 'getAXState')['state']
    raw['elements'].insert(0, {'role': 'AXButton', 'label': 'New', 'element_token': 's3:0'})
    changed = api.observe(state, 'getAXState')['state']
    assert '~ 0 button "New"' in changed and '+ 1 button (selected) "OK"' in changed
    assert api.target(state, 0) == {'element_token': 's3:0'}
    raw['elements'].reverse()
    changed = api.observe(state, 'getAXState')['state']
    assert '~ 0 button (selected) "OK"' in changed and '~ 1 button "New"' in changed
    raw['elements'].pop()
    assert 'Removed element IDs: 1' in api.observe(state, 'getAXState')['state']
    full = api.observe(state, 'getAXState', disable_diff=True)['state']
    assert '0 button (selected)' in full and 'AX changes' not in full
    raw['window_title'] = 'Renamed'
    assert 'no change' not in api.observe(state, 'getAXState')['state']
    raw['elements'] = []
    assert 'App AX unavailable' in api.observe(state, 'getAXState')['state']
    assert state['elements'] == {}
    assert 'App AX unavailable' in api.observe(state, 'getAXState')['state']
    raw['elements_complete'] = False
    changed = api.observe(state, 'getAXState')['state']
    assert 'incomplete' not in changed and 'App AX unavailable' in changed
    raw['degraded_reason'] = 'Permission denied'
    assert 'App AX unavailable' in api.observe(state, 'getAXState')['state']
    raw['pid'] = 99
    with pytest.raises(ValueError, match='WindowChanged'):
        api.observe(state, 'getAXState')


def test_app_scoping_compact_rendering_and_menu_expansion():
    elements = [
        {'role': 'AXWindow', 'label': 'Untitled', 'depth': 0},
        {'role': 'AXTextArea', 'value': 'hello\nworld', 'depth': 1, 'settable': True},
        {'role': 'AXMenuBar', 'depth': 0},
        {'role': 'AXMenuBarItem', 'label': 'Apple', 'depth': 1},
        {'role': 'AXMenu', 'depth': 2},
        {'role': 'AXMenuItem', 'label': 'Other app and recent files', 'depth': 3},
        {'role': 'AXMenuBarItem', 'label': 'File', 'depth': 1, 'selected': False},
        {'role': 'AXMenu', 'depth': 2, 'selected': False},
        {'role': 'AXMenuItem', 'label': 'Save', 'depth': 3, 'selected': False},
        {'role': 'AXMenuBarItem', 'label': 'Edit', 'depth': 1},
        {'role': 'AXMenuItem', 'label': 'Unknown visibility', 'depth': 2},
    ]
    visible = adapter.app_elements(elements)
    text = '\n'.join(adapter.ax_line(i, e) for i, e in enumerate(visible))
    assert 'Apple' not in text and 'Other app' not in text and 'Save' not in text
    assert '3 menu bar item "File"' in text and 'Unknown visibility' in text
    assert 'Value: "hello\\nworld"' in text
    elements[6]['selected'] = True
    assert elements[8] in adapter.app_elements(elements)
    elements[6]['selected'] = False
    elements[8]['selected'] = True
    assert elements[8] in adapter.app_elements(elements)
    line = adapter.ax_line(0, {'role': 'AXButton', 'label': 'Open', 'enabled': False,
        'actions': ['AXPress', 'AXShowMenu'], 'focused': True})
    assert line == '0 button (disabled, focused) "Open", Actions: show menu'


def test_active_menu_is_the_root_and_root_changes_return_full_state():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    raw = driver.responses['get_window_state']['structuredContent']
    window = {'role': 'AXWindow', 'label': 'Untitled', 'depth': 0, 'element_token': 's1:0'}
    text = {'role': 'AXTextArea', 'value': 'hello', 'depth': 1, 'element_token': 's1:1'}
    bar = {'role': 'AXMenuBar', 'depth': 0, 'element_token': 's1:2'}
    file_menu = {'role': 'AXMenuBarItem', 'label': 'File', 'depth': 1,
                 'selected': False, 'element_token': 's1:3'}
    file_child = {'role': 'AXMenuItem', 'label': 'Save', 'depth': 2,
                  'element_token': 's1:4'}
    window_menu = {'role': 'AXMenuBarItem', 'label': 'Window', 'depth': 1,
                   'selected': False, 'element_token': 's1:5'}
    menu = {'role': 'AXMenu', 'depth': 2, 'element_token': 's1:6'}
    minimize = {'role': 'AXMenuItem', 'label': 'Minimize', 'depth': 3,
                'element_token': 's1:7'}
    raw['elements'] = [window, text, bar, file_menu, file_child, window_menu, menu, minimize]

    initial = api.observe(state, 'getAXState')['state']
    assert '0 window "Untitled"' in initial and 'Minimize' not in initial

    window_menu['selected'] = True
    opened = api.observe(state, 'getAXState')['state']
    assert 'AX changes' not in opened
    assert '0 menu bar item (selected) "Window"' in opened
    assert '\t1 menu' in opened and '\t\t2 menu item "Minimize"' in opened
    assert 'window "Untitled"' not in opened and 'menu bar item "File"' not in opened
    assert api.target(state, 2) == {'element_token': 's1:7'}
    full = api.observe(state, 'getAXState', disable_diff=True)['state']
    assert '0 menu bar item (selected) "Window"' in full and 'AX changes' not in full

    window_menu['selected'] = False
    file_menu['selected'] = True
    switched = api.observe(state, 'getAXState')['state']
    assert 'AX changes' not in switched and '0 menu bar item (selected) "File"' in switched
    assert 'menu item "Save"' in switched and 'Minimize' not in switched

    file_menu['selected'] = False
    closed = api.observe(state, 'getAXState')['state']
    assert 'AX changes' not in closed and '0 window "Untitled"' in closed
    assert 'menu item "Save"' not in closed and 'Minimize' not in closed


def test_active_menu_takes_priority_over_an_attached_sheet():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    driver.responses['get_window_state']['structuredContent']['elements'] = [
        {'role': 'AXWindow', 'label': 'Untitled', 'depth': 0},
        {'role': 'AXSheet', 'label': 'save', 'depth': 1},
        {'role': 'AXButton', 'label': 'Save', 'depth': 2},
        {'role': 'AXMenuBar', 'depth': 0},
        {'role': 'AXMenuBarItem', 'label': 'Window', 'depth': 1, 'selected': True},
        {'role': 'AXMenu', 'depth': 2},
        {'role': 'AXMenuItem', 'label': 'Minimize', 'depth': 3},
    ]

    observed = api.observe(state, 'getAXState')['state']
    assert '0 menu bar item (selected) "Window"' in observed and 'Minimize' in observed
    assert 'sheet "save"' not in observed and 'window "Untitled"' not in observed


def test_app_and_window_diff_baselines_are_separate():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    first_app = {'pid': 42}
    second_app = {'pid': 99}
    assert '0 button' in api.observe(first_app, 'getAXState')['state']
    driver.responses['list_windows']['structuredContent']['windows'][0]['pid'] = 99
    assert '0 button' in api.observe(second_app, 'getAXState')['state']
    assert 'no change' in api.observe(second_app, 'getAXState')['state']
    driver.responses['list_windows']['structuredContent']['windows'][0]['window_id'] = 8
    assert '0 button' in api.observe(second_app, 'getAXState')['state']
    driver.responses['list_windows']['structuredContent']['windows'][0]['pid'] = 42
    driver.responses['list_windows']['structuredContent']['windows'][0]['window_id'] = 7
    assert 'no change' in api.observe(first_app, 'getAXState')['state']

def test_driver_disconnect_timeout_and_cleanup(tmp_path):
    fake = tmp_path / 'fake.py'
    fake.write_text('''import json, sys, time
for line in sys.stdin:
    r = json.loads(line)
    if 'id' not in r: continue
    if r['method'] == 'initialize': value = {}
    elif r['method'] == 'tools/list': value = {'tools': []}
    elif r['method'] == 'hang': time.sleep(10); continue
    else: sys.exit(0)
    print(json.dumps({'jsonrpc': '2.0', 'id': r['id'], 'result': value}), flush=True)
''')
    driver = adapter.McpClient([sys.executable, str(fake)], timeout=1)
    with pytest.raises(TimeoutError, match='outcome unknown'):
        driver.request('hang', {})
    assert driver.process.poll() is not None
    driver = adapter.McpClient([sys.executable, str(fake)], timeout=1)
    with pytest.raises(RuntimeError, match='connection failed'):
        driver.request('exit', {})
    assert driver.process.poll() is not None


def test_multiwindow_ax_reads_only_current_window_and_routes_actions(monkeypatch):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42}
    api.handles['app'] = state
    rows = driver.responses['list_windows']['structuredContent']['windows']
    rows[0].update(window_id=50, title='Save', z_index=3)
    rows.append({**rows[0], 'window_id': 60, 'title': 'Go to Folder', 'z_index': 49})
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            wid = args['window_id']
            response['structuredContent'].update(pid=42, window_id=wid,
                elements=[{'role': 'AXTextField', 'label': str(wid), 'element_token': f's{wid}:0'}])
        return response

    monkeypatch.setattr(driver, 'call', call)
    text = api.observe(state, 'getAXState')['state']
    assert 'Window: "Save"' not in text and 'Window: "Go to Folder"' in text
    assert 'text field "50"' not in text and '0 text field "60"' in text
    observed_ids = [args['window_id'] for name, args in driver.calls if name == 'get_window_state']
    assert observed_ids == [60]
    api.control('setValue', 'app', {'target': 0, 'value': '/tmp'})
    assert driver.calls[-1][0] == 'set_value'
    assert driver.calls[-1][1]['window_id'] == 60
    assert driver.calls[-1][1]['element_token'] == 's60:0'
    with pytest.raises(ValueError, match='StaleElement'):
        api.control('click', 'app', {'target': 0})
    api.observe(state, 'getAXState')
    rows.reverse()
    assert api.observe(state, 'getAXState')['state'].count('no change') == 1
    rows[:] = [w for w in rows if w['window_id'] != 50]
    text = api.observe(state, 'getAXState')['state']
    assert 'no change' in text and 'Closed window:' not in text

    def empty_or_failed(name, args):
        response = call(name, args)
        if name == 'get_window_state':
            if args['window_id'] == 60:
                response['structuredContent'].update(elements=[], degraded_reason='ax_window_unresolved')
            elif args['window_id'] == 70:
                raise RuntimeError('window closed during observation')
        return response

    monkeypatch.setattr(driver, 'call', empty_or_failed)
    rows.insert(0, {**rows[0], 'window_id': 50, 'title': 'Save', 'z_index': 3})
    text = api.observe(state, 'getAXState')['state']
    assert 'App AX unavailable' in text and 'Visible window:' not in text
    assert 'Windows without AX content' not in text
    assert 'Discard previous elements from these windows' not in text
    assert 'ax_window_unresolved' not in text
    with pytest.raises(ValueError, match='StaleElement'):
        api.target(state, 0)


def test_same_process_native_window_becomes_active_and_routes_actions(monkeypatch):
    driver = FakeDriver()
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = True
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    api.handles['app'] = state
    host = driver.responses['list_windows']['structuredContent']['windows'][0]
    host.update(window_id=33, z_index=24, is_focused=True)
    modal = [{'pid': 42, 'window_id': 42, 'title': '', 'layer': 8, 'z_index': 30,
              'is_on_screen': True, 'native_inventory': True,
              'bounds': {'x': 494, 'y': 186, 'width': 930, 'height': 448}}]
    monkeypatch.setattr(adapter, 'native_window_inventory', lambda pid: copy.deepcopy(modal))
    original = driver.call

    def call(name, args):
        if name == 'bring_to_front' and args['window_id'] == 42:
            pytest.fail('A verified active native window must not use layer-0 focus routing')
        response = original(name, args)
        if name == 'get_window_state':
            response['structuredContent'].update(pid=42, window_id=args['window_id'])
            if args['window_id'] == 42:
                response['structuredContent'].update(window_title='Modal', elements=[{
                    'role': 'AXWindow', 'label': 'Modal', 'depth': 0,
                    'element_token': 'modal:0',
                    'actions': ['AXPress'],
                    'frame': {'x': 494, 'y': 186, 'w': 930, 'h': 448}}])
        return response

    monkeypatch.setattr(driver, 'call', call)
    observed = api.observe(state, 'getAXState')['state']
    assert 'Window: "Modal"' in observed and 'Window ID: 42' in observed
    assert not any(name == 'bring_to_front' for name, _ in driver.calls)
    windows = api.control('listWindows', 'app', {})
    assert [window['id'] for window in windows] == [33, 42]

    api.observe(state, 'getAXStateAndScreenshot')
    assert ('bring_to_front', {'pid': 42, 'window_id': 42}) not in driver.calls
    api.control('click', 'app', {'target': 0})
    assert driver.calls[-1][0] == 'click'
    assert driver.calls[-1][1]['window_id'] == 42
    assert driver.calls[-1][1]['element_token'] == 'modal:0'
    api.control('pressKey', 'app', {'key': 'return'})
    assert driver.calls[-1][0] == 'press_key'
    api.control('paste', 'app', {'text': 'hello'})
    assert driver.calls[-1][0] == 'hotkey'
    assert ('bring_to_front', {'pid': 42, 'window_id': 42}) not in driver.calls

    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = False
    with pytest.raises(ValueError, match='focus was not verified'):
        api.observe(state, 'getAXStateAndScreenshot')
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = True

    modal.clear()
    before = len(driver.calls)
    with pytest.raises(ValueError, match='WindowChanged'):
        api.control('pressKey', 'app', {'key': 'return'})
    assert not any(name in {'bring_to_front', 'press_key'} for name, _ in driver.calls[before:])
    assert 'Window ID: 33' in api.observe(state, 'getAXState')['state']


def test_supplemental_file_click_uses_desktop_pixel_route(monkeypatch):
    driver = FakeDriver()
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = True
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    api.handles['app'] = state
    host = driver.responses['list_windows']['structuredContent']['windows'][0]
    host.update(window_id=33, z_index=24, is_focused=True)
    panel = {'pid': 42, 'window_id': 42, 'title': '', 'layer': 8, 'z_index': 30,
             'is_on_screen': True, 'native_inventory': True,
             'bounds': {'x': 494, 'y': 186, 'width': 930, 'height': 448}}
    monkeypatch.setattr(adapter, 'native_window_inventory', lambda pid: [copy.deepcopy(panel)])
    original = driver.call

    def call(name, args):
        if name == 'bring_to_front' and args['window_id'] == 42:
            pytest.fail('A verified supplemental window must not use layer-0 focus routing')
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 42:
            response['structuredContent'].update(
                pid=42, window_id=42, window_title='Microsoft Excel',
                window_bounds=copy.deepcopy(panel['bounds']), screenshot_width=930,
                screenshot_height=448, screenshot_scale=1.0, screenshot_frame_valid=True,
                background_input={
                    'exact_window': {'pid': 42, 'window_id': 42, 'status': 'matched'},
                    'routes': [{'route': 'window_pointer', 'status': 'available'}],
                },
                elements=[{
                    'role': 'AXWindow', 'label': 'Microsoft Excel', 'depth': 0,
                    'element_token': 'panel:0',
                    'frame': {'x': 494, 'y': 186, 'w': 930, 'h': 448},
                }, {
                    'role': 'AXTextField', 'label': 'input.xlsx', 'value': 'input.xlsx',
                    'depth': 1, 'element_token': 'panel:1',
                    'actions': ['AXOpen', 'AXShowMenu', 'AXConfirm'],
                    'frame': {'x': 919, 'y': 256, 'w': 64, 'h': 18},
                }])
        return response

    monkeypatch.setattr(driver, 'call', call)
    observed = api.observe(state, 'getAXState')['state']
    assert '1 text field "input.xlsx"' in observed

    start = len(driver.calls)
    api.control('click', 'app', {'target': 1})
    actions = [(name, args) for name, args in driver.calls[start:]
               if name in {'get_window_state', 'bring_to_front', 'click'}]
    clicks = [args for name, args in actions if name == 'click']
    assert len(clicks) == 1
    assert clicks[0] == {
        'scope': 'desktop', 'x': 951.0, 'y': 265.0, 'button': 'left',
        'count': 1, 'delivery_mode': 'foreground', 'session': api.session,
    }
    assert any(name == 'get_window_state' and args['window_id'] == 42
               and args['include_screenshot'] is True
               and args['include_accessibility_tree'] is False for name, args in actions)
    assert not any(name == 'bring_to_front' for name, _ in actions)

    api.observe(state, 'getAXStateAndScreenshot')
    start = len(driver.calls)
    api.control('click', 'app', {'target': [470, 80]})
    clicks = [args for name, args in driver.calls[start:] if name == 'click']
    assert len(clicks) == 1
    assert clicks[0]['scope'] == 'desktop'
    assert clicks[0]['x'] == 964 and clicks[0]['y'] == 266
    assert clicks[0]['delivery_mode'] == 'foreground'
    assert not any(name == 'bring_to_front' and args['window_id'] == 42
                   for name, args in driver.calls[start:])


def test_native_inventory_accepts_verified_windows_on_any_layer(monkeypatch):
    driver = FakeDriver()
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = True
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    monkeypatch.setattr(adapter, 'native_window_inventory', lambda pid: [
        {'pid': 99, 'window_id': 40, 'layer': 8, 'z_index': 50, 'is_on_screen': True,
         'native_inventory': True, 'bounds': {'width': 500, 'height': 400}},
        {'pid': 42, 'window_id': 41, 'layer': 3, 'z_index': 49, 'is_on_screen': True,
         'native_inventory': True, 'bounds': {'width': 500, 'height': 400}},
    ])
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            response['structuredContent'].update(
                pid=42, window_id=args['window_id'],
                elements=[{'role': 'AXWindow', 'depth': 0}])
        return response

    monkeypatch.setattr(driver, 'call', call)
    assert api.windows(state)['window_id'] == 41


def test_unverified_native_surface_is_excluded_before_selection(monkeypatch):
    driver = FakeDriver()
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = True
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    api.handles['app'] = state
    monkeypatch.setattr(adapter, 'native_window_inventory', lambda pid: [{
        'pid': 42, 'window_id': 42, 'layer': 8, 'z_index': 50, 'is_on_screen': True,
        'native_inventory': True, 'bounds': {'width': 500, 'height': 400}}])
    assert api.windows(state)['window_id'] == 7
    assert [window['id'] for window in api.control('listWindows', 'app', {})] == [7]

    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 42:
            response['structuredContent'].update(
                pid=42, window_id=42, elements=[{'role': 'AXWindow', 'depth': 0}])
        return response

    monkeypatch.setattr(driver, 'call', call)
    assert [window['id'] for window in api.control('listWindows', 'app', {})] == [7, 42]


def test_screenshot_validates_native_surface_before_focus(monkeypatch):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42}
    monkeypatch.setattr(adapter, 'native_window_inventory', lambda pid: [{
        'pid': 42, 'window_id': 42, 'layer': 3, 'z_index': 50,
        'is_on_screen': True, 'native_inventory': True,
        'bounds': {'x': 100, 'y': 100, 'width': 500, 'height': 400}}])

    api.observe(state, 'getScreenshot')

    assert ('bring_to_front', {'pid': 42, 'window_id': 7}) in driver.calls
    assert ('bring_to_front', {'pid': 42, 'window_id': 42}) not in driver.calls
    assert any(name == 'get_window_state' and args['window_id'] == 42
               and args['include_accessibility_tree'] is True
               for name, args in driver.calls)


def test_new_unverified_native_surface_blocks_input_until_observed(monkeypatch):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42}
    api.handles['app'] = state
    native = []
    monkeypatch.setattr(adapter, 'native_window_inventory', lambda pid: copy.deepcopy(native))
    api.observe(state, 'getAXState')
    native.append({
        'pid': 42, 'window_id': 42, 'layer': 3, 'z_index': 50,
        'is_on_screen': True, 'native_inventory': True,
        'bounds': {'x': 100, 'y': 100, 'width': 500, 'height': 400}})

    before = len(driver.calls)
    with pytest.raises(ValueError, match='WindowUnverified'):
        api.control('pressKey', 'app', {'key': 'return'})
    assert not any(name in {'bring_to_front', 'hotkey'} for name, _ in driver.calls[before:])


def test_current_window_keeps_its_full_tree_when_it_contains_a_sheet():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42}
    rows = driver.responses['list_windows']['structuredContent']['windows']
    rows[0].update(window_id=41, title='Untitled')
    raw = driver.responses['get_window_state']['structuredContent']
    raw['elements'] = [
        {'role': 'AXButton', 'label': 'Document', 'depth': 0, 'element_token': 's41:0'},
        {'role': 'AXSheet', 'label': 'save', 'depth': 0},
        {'role': 'AXButton', 'label': 'Save', 'depth': 1, 'element_token': 's41:1'},
    ]

    text = api.observe(state, 'getAXState')['state']

    assert 'Window: "Untitled"' in text
    assert '0 button "Document"' in text
    assert '1 sheet "save"' in text and '2 button "Save"' in text
    assert [args['window_id'] for name, args in driver.calls if name == 'get_window_state'] == [41]
    assert api.target(state, 2) == {'element_token': 's41:1'}


def test_display_only_static_text_labels_addressable_parent():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42}
    raw = driver.responses['get_window_state']['structuredContent']
    raw['elements'] = [
        {'role': 'AXWindow', 'label': 'Open', 'depth': 0, 'element_index': 0,
         'element_token': 's1:0'},
        {'role': 'AXRow', 'depth': 1, 'element_index': 1, 'element_token': 's1:1'},
        {'role': 'AXRow', 'depth': 1, 'selected': True, 'element_index': 2,
         'element_token': 's1:2'},
        {'role': 'AXCell', 'depth': 2, 'element_index': 3, 'element_token': 's1:3'},
    ]
    driver.responses['get_window_state']['content'] = [{'type': 'text', 'text': '''
- [0] AXWindow "Open"
  - [1] AXRow
    - AXStaticText = "个人收藏"
  - [2] AXRow [selected=true]
    - [3] AXCell [actions=[open]]
      - AXStaticText = "应用程序"
'''}]

    text = api.observe(state, 'getAXState')['state']

    assert '1 row "个人收藏"' in text
    assert '2 cell (selected) "应用程序"' in text
    assert 'row (selected)' not in text
    assert api.target(state, 2) == {'element_token': 's1:3'}


@pytest.mark.parametrize('method,args,tool', [
    ('click', {'target': 1}, 'click'),
    ('click', {'target': 1, 'options': {'clickCount': 2}}, 'double_click'),
    ('setValue', {'target': 1, 'value': 'report.txt'}, 'set_value'),
    ('scroll', {'target': 1, 'direction': 'down'}, 'scroll'),
])
def test_sheet_element_source_is_separate_from_active_window(monkeypatch, method, args, tool):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42}
    api.handles['app'] = state
    rows = driver.responses['list_windows']['structuredContent']['windows']
    rows[0].update(window_id=41, title='Untitled', z_index=35)
    rows.append({**rows[0], 'window_id': 55, 'title': 'Save', 'z_index': 45,
                 'bounds': {'x': 501, 'y': 221, 'width': 458, 'height': 248}})
    original = driver.call

    def call(name, arguments):
        response = original(name, arguments)
        if name == 'get_window_state':
            wid = arguments['window_id']
            response['structuredContent'].update(pid=42, window_id=wid)
            if wid == 41:
                response['structuredContent']['elements'] = [
                    {'role': 'AXButton', 'label': 'Document', 'depth': 0, 'element_token': 's41:0'},
                        {'role': 'AXSheet', 'label': 'save', 'depth': 0,
                         'frame': {'x': 501, 'y': 221, 'w': 458, 'h': 248}},
                        {'role': 'AXButton', 'label': 'OK', 'depth': 1,
                         'element_token': 's41:1', 'actions': ['AXPress']},
                ]
            else:
                response['structuredContent'].update(elements=[], degraded_reason='ax_window_unresolved')
        if name == 'bring_to_front' and arguments['window_id'] == 41:
            pytest.fail('The document must not be activated to act on its attached sheet')
        return response

    monkeypatch.setattr(driver, 'call', call)
    observed = api.observe(state, 'getAXStateAndScreenshot')
    assert 'Attached sheet: "Save"' in observed['state']
    assert '0 sheet "save"' in observed['state'] and '1 button "OK"' in observed['state']
    assert 'Document' not in observed['state']
    assert 'Target window ID:' not in observed['state']
    assert 'Screenshot window ID' not in observed['state']
    assert observed['image'] is not None
    assert state['image_frame'][0] == 55
    api.control(method, 'app', args)
    assert ('bring_to_front', {'pid': 42, 'window_id': 55}) in driver.calls
    assert driver.calls[-1][0] == tool
    assert driver.calls[-1][1]['window_id'] == 41
    assert driver.calls[-1][1]['element_token'] == 's41:1'

    api.observe(state, 'getAXState')
    driver.responses['bring_to_front'] = result(exact_window_effect={'verified': False})
    before = len(driver.calls)
    with pytest.raises(ValueError, match='focus was not verified'):
        api.control(method, 'app', args)
    assert not any(name == tool for name, _ in driver.calls[before:])
    driver.responses['bring_to_front'] = result(exact_window_effect={'verified': True})
    api.observe(state, 'getAXState')
    driver.responses[tool] = {'isError': True, 'content': [
        {'type': 'text', 'text': 'driver rejected sheet ownership'}]}
    with pytest.raises(adapter.ActionError, match='driver rejected sheet ownership'):
        api.control(method, 'app', args)
    del driver.responses[tool]
    api.observe(state, 'getAXState')
    rows.append({**rows[-1], 'window_id': 61, 'z_index': 55})
    before = len(driver.calls)
    with pytest.raises(ValueError, match='WindowChanged'):
        api.control(method, 'app', args)
    assert not any(name in {tool, 'bring_to_front'} for name, _ in driver.calls[before:])
    rows.pop()
    api.observe(state, 'getAXState')
    rows.pop(0)
    before = len(driver.calls)
    with pytest.raises(ValueError, match='WindowChanged'):
        api.control(method, 'app', args)
    assert not any(name in {tool, 'bring_to_front'} for name, _ in driver.calls[before:])


def test_app_ax_returns_only_matching_attached_sheet_and_preserves_evidence(monkeypatch):
    driver = FakeDriver()
    evidence = io.StringIO()
    api = adapter.Adapter(driver, evidence=evidence)
    state = {'pid': 42}
    rows = driver.responses['list_windows']['structuredContent']['windows']
    rows[0].update(window_id=41, z_index=35)
    panel = {**rows[0], 'window_id': 55, 'title': 'Save', 'z_index': 45,
             'bounds': {'x': 501, 'y': 221, 'width': 458, 'height': 248}}
    rows.append(panel)
    sheet = {'role': 'AXSheet', 'label': 'save', 'depth': 0,
             'frame': {'x': 501, 'y': 221, 'w': 458, 'h': 248}}
    reason = 'ax_window_unresolved: window 55 has no AXWindow'
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            response['structuredContent'].update(window_id=args['window_id'])
            if args['window_id'] == 41:
                response['structuredContent']['elements'] = [
                    {'role': 'AXButton', 'label': 'Document', 'depth': 0, 'element_token': 's41:0'}, sheet,
                    {'role': 'AXButton', 'label': 'Save', 'depth': 1, 'element_token': 's41:1'}]
            else:
                response['structuredContent'].update(elements=[], degraded_reason=reason)
        return response

    monkeypatch.setattr(driver, 'call', call)
    text = api.observe(state, 'getAXStateAndScreenshot')['state']
    assert 'Attached sheet: "Save"' in text and '1 button "Save"' in text
    assert 'Document' not in text and 'AX unavailable' not in text
    assert 'Target window ID:' not in text and 'Screenshot window ID' not in text
    assert state['image_frame'][0] == 55
    assert 'Windows without AX content' not in text
    assert 'Visible window:' not in text
    assert reason in evidence.getvalue()
    assert 'AX unavailable' not in api.observe(state, 'getAXState')['state']
    sheet['frame']['x'] += 3
    unmatched = api.observe(state, 'getAXState')['state']
    assert 'App AX unavailable' in unmatched and 'Document' not in unmatched
    sheet['frame']['x'] -= 3
    reason = 'Permission denied'
    assert reason not in api.observe(state, 'getAXState')['state']
    reason = 'ax_window_unresolved'
    panel['title'] = 'Another panel'
    assert reason not in api.observe(state, 'getAXState')['state']

    # When the remaining content vanishes, report one app-level failure, not per-window errors.
    rows.pop(0)
    text = api.observe(state, 'getAXState')['state']
    assert 'App AX unavailable: no accessible elements' in text
    assert 'Window:' not in text and reason not in text
    assert state['elements'] == {}
    assert reason in evidence.getvalue()


@pytest.mark.parametrize('method,args,tool', [
    ('pressKey', {'key': 'super+shift+g'}, 'hotkey'),
    ('paste', {'text': 'hello'}, 'hotkey'),
    ('typeText', {'text': 'hello'}, 'type_text'),
    ('setValue', {'target': 0, 'value': 'hello'}, 'set_value'),
])
def test_focus_evidence_corrects_rank_before_input(monkeypatch, method, args, tool):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42}
    api.handles['app'] = state
    rows = driver.responses['list_windows']['structuredContent']['windows']
    rows[0].update(window_id=41, z_index=35)
    rows.append({**rows[0], 'window_id': 55, 'title': 'Save', 'z_index': 8})
    api.observe(state, 'getAXStateAndScreenshot')
    rejection = {'isError': True, **result(
        code='bring_to_front_exact_window_unverified', pid=42, window_id=41,
        exact_window_effect={'verified': False}, observed={
            'focused_window_id': 55, 'frontmost_ordinary_window_id': 55,
            'front_process_matches_target': True, 'frontmost_pid': 42,
            'workspace_frontmost_pid': 42})}
    original = driver.call

    def call(name, arguments):
        response = original(name, arguments)
        if name == 'bring_to_front' and arguments['window_id'] == 41:
            return rejection
        return response

    monkeypatch.setattr(driver, 'call', call)
    start = len(driver.calls)
    api.control(method, 'app', args)
    actions = [(n, a) for n, a in driver.calls[start:] if n == 'bring_to_front' or n == tool]
    assert [a['window_id'] for n, a in actions if n == 'bring_to_front'] == ([41, 55, 55] if method == 'paste' else [41, 55])
    assert sum(n == tool for n, _ in actions) == 1
    if tool != 'hotkey':
        assert actions[-1][1]['window_id'] == (41 if tool == 'set_value' else 55)
    assert 'image_frame' not in state
    assert 'Window ID: 55' in api.observe(state, 'getAXState')['state']

    # Coordinates never migrate to the corrected surface; foreign focus is never followed.
    state.pop('focus_hint')
    monkeypatch.setattr(driver, 'call', original)
    api.observe(state, 'getAXStateAndScreenshot')
    monkeypatch.setattr(driver, 'call', call)
    start = len(driver.calls)
    with pytest.raises(adapter.ActionError):
        api.control('click', 'app', {'target': [1, 1]})
    assert not any(n == 'click' for n, _ in driver.calls[start:])
    rejection['structuredContent']['observed']['frontmost_pid'] = 99
    with pytest.raises(adapter.ActionError):
        api.control('pressKey', 'app', {'key': 'Return'})
    rejection['structuredContent']['observed']['frontmost_pid'] = 42
    driver.responses['bring_to_front'] = result(exact_window_effect={'verified': False})
    start = len(driver.calls)
    with pytest.raises(ValueError, match='correction was not verified'):
        api.control('pressKey', 'app', {'key': 'Return'})
    assert not any(n == 'press_key' for n, _ in driver.calls[start:])
    assert sum(n == 'bring_to_front' for n, _ in driver.calls[start:]) == 2


@pytest.mark.parametrize('tool', ['launch_app', 'press_key'])
def test_ended_session_revives_reported_identity_once_and_records_attempts(tool):
    driver = FakeDriver()
    evidence = io.StringIO()
    api = adapter.Adapter(driver, evidence)
    session = 'mcp-1358-1790063566694593000' if tool == 'launch_app' else api.session
    rejection = {'isError': True, 'content': [{'type': 'text', 'text':
        f"session '{session}' has ended; tool call '{tool}' was rejected. "
        'Call start_session with this id to revive it before issuing further actions, or use a new session id.'}]}
    original = driver.call
    def call(name, args):
        response = original(name, args)
        if name == tool and sum(n == tool for n, _ in driver.calls) == 1:
            return rejection
        return response
    driver.call = call
    args = {'name': 'Finder'} if tool == 'launch_app' else {'key': 'return'}
    api.call(tool, args)
    assert [n for n, _ in driver.calls] == [tool, 'start_session', tool]
    assert driver.calls[1][1] == {'session': session}
    assert driver.calls[0][1] == driver.calls[2][1]
    rows = [json.loads(line) for line in evidence.getvalue().splitlines()]
    assert len(rows) == 6 and rows[1]['result'] == rejection

    # An unsuccessful revival or a second rejection must stop, never loop.
    driver.call = original
    driver.responses[tool] = rejection
    driver.calls.clear()
    with pytest.raises(adapter.ActionError):
        api.call(tool, args)
    assert [n for n, _ in driver.calls] == [tool, 'start_session', tool]
    driver.responses['start_session'] = {'isError': True, 'content': []}
    driver.calls.clear()
    with pytest.raises(adapter.ActionError):
        api.call(tool, args)
    assert [n for n, _ in driver.calls] == [tool, 'start_session']


@pytest.mark.parametrize('failure', ['timeout', 'unknown', 'other_error'])
def test_session_recovery_never_replays_uncertain_actions(failure):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    original = driver.call
    def call(name, args):
        original(name, args)
        if failure == 'timeout':
            raise TimeoutError('outcome unknown')
        return {'isError': True, 'structuredContent': {'effect': 'unknown'},
                'content': [{'type': 'text', 'text': failure}]}
    driver.call = call
    with pytest.raises((TimeoutError, adapter.ActionError)):
        api.call('press_key', {'key': 'return'})
    assert len(driver.calls) == 1


def _open_panel_api(monkeypatch, w01_frame):
    # Box-Agent W01 trace 2026-10-11: Open-panel column entries expose only AXOpen
    # (driver returns -25205 without navigating) and W01 sat below the browser's
    # visible rect, so its frame centre clicked empty space under the columns.
    driver = FakeDriver()
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = True
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    api.handles['app'] = state
    host = driver.responses['list_windows']['structuredContent']['windows'][0]
    host.update(window_id=33, z_index=24)
    panel = {'pid': 42, 'window_id': 42, 'title': '', 'layer': 8, 'z_index': 30,
             'is_on_screen': True, 'native_inventory': True,
             'bounds': {'x': 289, 'y': 144, 'width': 933, 'height': 448}}
    monkeypatch.setattr(adapter, 'native_window_inventory', lambda pid: [
        copy.deepcopy(panel), {**copy.deepcopy(host), 'native_inventory': True, 'z_index': 20}])
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 42:
            response['structuredContent'].update(
                pid=42, window_id=42, window_title='Microsoft Word',
                window_bounds=copy.deepcopy(panel['bounds']), screenshot_width=933,
                screenshot_height=448, screenshot_scale=1.0, screenshot_frame_valid=True,
                background_input={'exact_window': {'pid': 42, 'window_id': 42, 'status': 'matched'}},
                elements=[
                    {'role': 'AXWindow', 'label': 'Microsoft Word', 'depth': 0, 'element_token': 'p:0',
                     'frame': {'x': 289, 'y': 144, 'w': 933, 'h': 448}},
                    {'role': 'AXBrowser', 'label': '分栏视图', 'depth': 1, 'element_token': 'p:1',
                     'frame': {'x': 288, 'y': 208, 'w': 935, 'h': 282}},
                    {'role': 'AXList', 'depth': 2, 'element_token': 'p:2',
                     'frame': {'x': 751, 'y': 130, 'w': 246, 'h': 479}},
                    {'role': 'AXTextField', 'label': 'W01', 'value': 'W01', 'depth': 3,
                     'element_token': 'p:3', 'actions': ['AXOpen', 'AXShowMenu', 'AXConfirm'],
                     'frame': w01_frame},
                ])
        return response

    monkeypatch.setattr(driver, 'call', call)
    api.observe(state, 'getAXState')
    return api, driver
