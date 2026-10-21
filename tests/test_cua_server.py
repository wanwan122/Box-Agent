"""Box-Agent MCP tool layer over the ported adapter (fake driver; no GUI/model).

Re-expresses the mac-cua JS REPL contracts from tests/test_cua_adapter.py as
direct tool calls, against the cua-driver 0.25.0 foreground contract.
"""
import asyncio
import copy

import pytest

from box_agent.mcp_servers import cua_adapter as adapter
from box_agent.mcp_servers import cua_server

from tests.test_cua_adapter_port import FakeDriver, PNG, result


@pytest.fixture
def service():
    drivers = []

    def factory(timeout):
        driver = FakeDriver()
        driver.tools['get_window_state']['inputSchema']['properties']['max_image_dimension'] = {}
        driver.tools['get_screen_size'] = {'name': 'get_screen_size', 'inputSchema': {
            'type': 'object', 'properties': {'session': {}}}}
        driver.responses['get_screen_size'] = result(width=1512, height=982, scale_factor=2.0)
        drivers.append(driver)
        return driver

    svc = cua_server.CuaService(factory)
    svc.drivers = drivers
    yield svc
    svc.close()


def ok(service, name, **args):
    content, is_error = service.call(name, args)
    assert not is_error, content
    return content


def texts(content):
    return '\n'.join(c['text'] for c in content if c['type'] == 'text')


def test_get_app_observation_diff_and_silent_actions(service):
    initial = ok(service, 'get_app', app='TextEdit')
    assert [c['type'] for c in initial] == ['text']
    assert texts(initial).startswith('Window:') and '0 button "OK"' in texts(initial)
    combined = ok(service, 'get_state', app='TextEdit', screenshot=True, disable_diffing=True)
    assert [c['type'] for c in combined] == ['text', 'image']
    assert combined[1] == {'type': 'image', 'mimeType': 'image/png', 'data': PNG}
    only_image = ok(service, 'get_state', app='TextEdit', screenshot=True, ax=False)
    assert [c['type'] for c in only_image] == ['image']
    driver = service.drivers[0]
    driver.responses['get_window_state']['structuredContent']['elements'][0]['label'] = 'Changed'
    assert '~ 0 button "Changed"' in texts(ok(service, 'get_state', app='TextEdit'))
    assert 'no change' in texts(ok(service, 'get_state', app='TextEdit'))
    before = len(driver.calls)
    assert texts(ok(service, 'click', app='TextEdit', target=0)).startswith('Done')
    ok(service, 'type_text', app='TextEdit', text='hello')
    ok(service, 'press_key', app='TextEdit', key='cmd+s')
    sent = [n for n, _ in driver.calls[before:]]
    assert 'get_window_state' not in sent and sent[-1] == 'hotkey'
    assert driver.calls[-1][1]['keys'] == ['cmd', 's']


def test_unknown_app_reference_and_stale_ids_send_no_input(service):
    content, is_error = service.call('click', {'app': 'Word', 'target': 0})
    assert is_error and 'get_app' in texts(content)
    ok(service, 'get_app', app='TextEdit')
    ok(service, 'click', app='TextEdit', target=0)
    driver = service.drivers[0]
    before = len(driver.calls)
    content, is_error = service.call('click', {'app': 'TextEdit', 'target': 0})
    assert is_error and 'StaleElement' in texts(content)
    assert not any(n == 'click' for n, _ in driver.calls[before:])
    content, is_error = service.call('click', {'app': 'TextEdit', 'target': [12, 34]})
    assert is_error and 'Coordinates require a current screenshot' in texts(content)


def test_failed_effects_are_errors_and_never_replayed(service):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    driver.responses['type_text'] = result(effect='partial')
    content, is_error = service.call('type_text', {'app': 'TextEdit', 'text': 'x'})
    assert is_error and 'partial' in texts(content)
    assert sum(n == 'type_text' for n, _ in driver.calls) == 1


def test_transport_failure_resets_references(service):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]

    def boom(name, args):
        driver.calls.append((name, args))
        raise TimeoutError('Driver timed out; outcome unknown.')

    driver.call = boom
    content, is_error = service.call('press_key', {'app': 'TextEdit', 'key': 'Return'})
    assert is_error and 'do not replay' in texts(content)
    assert service.adapter is None and driver.closed
    content, is_error = service.call('get_state', {'app': 'TextEdit'})
    assert is_error and 'get_app' in texts(content)
    ok(service, 'get_app', app='TextEdit')
    assert len(service.drivers) == 2


def test_paste_writes_clipboard_once_then_cmd_v(service):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    driver.calls.clear()
    ok(service, 'paste', app='TextEdit', text='/Users/me/报告.docx')
    actions = [(n, a) for n, a in driver.calls if n in {'bring_to_front', 'clipboard_write', 'hotkey'}]
    assert [n for n, _ in actions] == ['bring_to_front', 'clipboard_write', 'bring_to_front', 'hotkey']
    assert actions[1][1]['text'] == '/Users/me/报告.docx'
    assert actions[-1][1] == {'scope': 'desktop', 'keys': ['cmd', 'v'], 'session': service.adapter.session}


def test_schema_negotiated_screenshot_size_is_capped_to_model_image_limit(service):
    ok(service, 'get_app', app='TextEdit')
    ok(service, 'get_state', app='TextEdit', screenshot=True)
    observed = [a for n, a in service.drivers[0].calls if n == 'get_window_state']
    assert observed and all(a['max_image_dimension'] == adapter.MAX_IMAGE_DIMENSION for a in observed)


def test_025_supplemental_desktop_click_uses_logical_screen_points(service):
    ok(service, 'get_app', app='TextEdit')
    api = service.adapter
    state = {'pid': 42, 'image_background_input': {'exact_window': {
        'pid': 42, 'window_id': 55, 'status': 'matched'}},
        'image_bounds': (100.0, 200.0, 400.0, 300.0), 'image_size': (800, 600)}
    window = {'window_id': 55, 'native_inventory': True, 'native_verified': True}
    identity, params = api.pixel_click_route(state, window, {'x': 400, 'y': 300})
    # CuaDriver 0.25.0 desktop input uses logical screen points.
    assert identity == {'scope': 'desktop'}
    assert (params['x'], params['y']) == (300.0, 350.0)


@pytest.mark.parametrize('label', ['raccoon-docx', 'repl-0123'])
def test_025_ended_session_message_revives_once(label):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    rejection = {'isError': True, 'content': [{'type': 'text', 'text':
        f"session '{label}' has ended; tool call 'press_key' was rejected. "
        "Call start_session with this id to revive it before issuing further actions, or use a new session id."}]}
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'press_key' and sum(n == 'press_key' for n, _ in driver.calls) == 1:
            return rejection
        return response

    driver.call = call
    api.call('press_key', {'key': 'return'})
    assert [n for n, _ in driver.calls] == ['press_key', 'start_session', 'press_key']
    assert driver.calls[1][1] == {'session': label}


def test_unnamed_032_session_rejection_is_not_revived():
    text = ("session has ended; tool call 'press_key' was rejected. "
            "Call start_session without a session label to start a new unnamed session.")
    assert adapter.ended_session('press_key', text) is None


def test_mcp_server_lists_tools_and_returns_images(service):
    server = cua_server.build_server(service)
    from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

    async def run():
        listed = await server.request_handlers[ListToolsRequest](ListToolsRequest(method='tools/list'))
        names = [t.name for t in listed.root.tools]
        call = server.request_handlers[CallToolRequest]
        await call(CallToolRequest(method='tools/call', params=CallToolRequestParams(
            name='get_app', arguments={'app': 'TextEdit'})))
        state = await call(CallToolRequest(method='tools/call', params=CallToolRequestParams(
            name='get_state', arguments={'app': 'TextEdit', 'screenshot': True})))
        bad = await call(CallToolRequest(method='tools/call', params=CallToolRequestParams(
            name='click', arguments={'app': 'TextEdit', 'target': 'OK'})))
        return names, state.root, bad.root

    names, state, bad = asyncio.run(run())
    assert names == ['list_apps', 'get_app', 'list_windows', 'get_state', 'click', 'drag',
                     'scroll', 'set_value', 'type_text', 'paste', 'press_key']
    assert not state.isError and [c.type for c in state.content] == ['text', 'image']
    assert bad.isError


def _desktop_driver(service, width=1568, height=1018, original=(3024, 1964)):
    ok(service, 'list_apps')
    driver = service.drivers[0]
    driver.tools['get_desktop_state'] = {'name': 'get_desktop_state', 'inputSchema': {
        'type': 'object', 'properties': {'max_image_dimension': {}, 'session': {}}}}
    driver.responses['get_desktop_state'] = {
        'content': [{'type': 'image', 'mimeType': 'image/png', 'data': PNG}],
        'structuredContent': {'screenshot_width': width, 'screenshot_height': height,
                              'screenshot_original_width': original[0],
                              'screenshot_original_height': original[1], 'scale_factor': 2.0}}
    return driver
