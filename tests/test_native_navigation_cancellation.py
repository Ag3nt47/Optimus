"""Event-compatible worker cancellation and the real slowdown-review call path.

Only fake resource snapshots and private Python events are used. No real process
inventory, process controls, installers, or maintenance operations run here.
"""
import threading
from unittest.mock import Mock

import pytest

from optimus import navigation, slowdown
from optimus.backend import Backend
from optimus.navigation import NavigationHub


def worker():
    global_stop=threading.Event()
    hub=NavigationHub();hub.begin('1'*32,cancel_event=global_stop)
    cursor=hub.cursor('maintenance');assert cursor.begin_item('slowdown_review','Measure application pressure')
    return hub,cursor,global_stop


def request(hub,cursor,action):
    state=hub.snapshot()
    row=next(job for job in state['jobs'] if job['id']==cursor.id)
    return hub.request(state['session_id'],cursor.id,row['revision'],action)


@pytest.mark.parametrize('timeout',[0,-1,float('nan')])
def test_nonpositive_wait_matches_event_without_cancelling(timeout):
    _,cursor,global_stop=worker()
    token=cursor.cancellation()
    assert token.wait(timeout) is threading.Event().wait(timeout) is False
    assert not token.is_set() and not cursor.local_requested() and not global_stop.is_set()


@pytest.mark.parametrize('timeout',[float('inf'),threading.TIMEOUT_MAX+1])
def test_oversized_wait_matches_event_overflow(timeout):
    _,cursor,_=worker()
    with pytest.raises(OverflowError):threading.Event().wait(timeout)
    with pytest.raises(OverflowError):cursor.cancellation().wait(timeout)


def test_timeout_uses_bounded_monotonic_slices(monkeypatch):
    _,cursor,_=worker()
    clock=[10.0];waits=[]
    monkeypatch.setattr(navigation.time,'monotonic',lambda:clock[0])
    def wait(delay):waits.append(delay);clock[0]+=delay;return False
    monkeypatch.setattr(cursor._item_stop,'wait',wait)
    assert cursor.cancellation().wait(0.12) is False
    assert len(waits)==3 and all(0<value<=0.05 for value in waits)
    assert sum(waits)==pytest.approx(0.12)


@pytest.mark.parametrize('source',['next','cancel','global','external','lane_end','hub_end'])
def test_indefinite_wait_wakes_for_each_owner_without_holding_lock(source,monkeypatch):
    hub,cursor,global_stop=worker();external=threading.Event()
    token=cursor.cancellation(external);entered=threading.Event();result=[]
    original=cursor._item_stop.wait
    def observed(delay):entered.set();return original(delay)
    monkeypatch.setattr(cursor._item_stop,'wait',observed)
    thread=threading.Thread(target=lambda:result.append(token.wait()),daemon=True)
    thread.start()
    try:
        assert entered.wait(2),'Cancellation waiter did not reach its bounded sleep.'
        assert hub.lock.acquire(blocking=False),'Sleeping must release navigation lock so controls remain usable.'
        hub.lock.release()
        if source in ('next','cancel'):assert request(hub,cursor,source)['accepted']
        elif source=='global':global_stop.set()
        elif source=='external':external.set()
        elif source=='lane_end':cursor.end()
        else:hub.end()
        thread.join(2)
        assert not thread.is_alive() and result==[True]
        if source in ('global','external'):
            assert not cursor.local_requested(),'Global/external cancellation must remain distinguishable from Next.'
    finally:
        global_stop.set();cursor._item_stop.set();thread.join(2)


def test_local_wait_does_not_cancel_sibling_or_next_item():
    hub,cursor,global_stop=worker()
    sibling=hub.cursor('driver_updates');assert sibling.begin_item('scan','Driver scan')
    assert request(hub,cursor,'next')['accepted']
    assert cursor.cancellation().wait(0) is True
    assert sibling.cancellation().wait(0) is False and not global_stop.is_set()
    cursor.finish('skipped',mutation_started=False)
    assert cursor.begin_item('next_review','Next independent review')
    assert cursor.cancellation().wait(0) is False


def frame(index):
    identity=(100,1.0,'fixture-app')
    return {'time':float(index),'processes':{identity:{'pid':100,'name':'Fixture app','exe':'fixture-app',
        'memory_rss':100*1024**2,'cpu':float(index)*2,'read':0,'write':0}},
        'inaccessible':0,'cpu_total':float(index)*4,'cpu_idle':float(index)*2,
        'memory':{'total':8*1024**3,'available':4*1024**3,'percent':50.0}}


def production_backend():
    # This production method requires no initialized state. Skip the constructor
    # entirely so the fixture creates no bridges, preferences, samplers or services.
    return object.__new__(Backend)


def test_production_slowdown_review_completes_with_navigation_event(monkeypatch):
    _,cursor,global_stop=worker()
    capture=Mock(side_effect=[frame(1),frame(2),frame(3)])
    monkeypatch.setattr(slowdown,'_capture',capture)
    monkeypatch.setattr(slowdown.psutil,'cpu_count',lambda logical=True:4)
    result=production_backend().slowdown_report(cancel_event=cursor.cancellation(global_stop))
    assert capture.call_count==3 and result['sample_count']==3
    assert result['complete'] is True and result['cancelled'] is False
    assert result['applications'][0]['cpu_percent']==50.0
    assert result['applications'][0]['severity']=='high'
    assert not cursor.local_requested() and not global_stop.is_set()


@pytest.mark.parametrize('source',['next','cancel','global','external'])
def test_production_slowdown_review_stops_with_partial_evidence(source,monkeypatch):
    hub,cursor,global_stop=worker();external=threading.Event()
    def capture():
        if source in ('next','cancel'):assert request(hub,cursor,source)['accepted']
        elif source=='global':global_stop.set()
        else:external.set()
        return frame(1)
    captured=Mock(side_effect=capture)
    monkeypatch.setattr(slowdown,'_capture',captured)
    monkeypatch.setattr(slowdown.psutil,'cpu_count',lambda logical=True:4)
    result=production_backend().slowdown_report(cancel_event=cursor.cancellation(external))
    assert captured.call_count==1 and result['sample_count']==1
    assert result['cancelled'] is True and result['complete'] is False
    assert cursor.local_requested() is (source in ('next','cancel'))
