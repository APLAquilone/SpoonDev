"""Exercise the shipped browser functions with virtual time and delayed fetches."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8').split('\n');
function line(prefix) {
  const value = source.find(item => item.startsWith(prefix));
  if (!value) throw Error('Missing browser function: ' + prefix);
  return value;
}
async function browser(kind, delays, options = {}) {
  let now = 0, serial = 0;
  const events = [], listeners = new Map(), elements = new Map();
  const result = {started: 0, completed: 0, aborted: 0, rendered: 0, ids: [], messages: []};
  function schedule(fn, ms, repeat = 0) {
    const event = {at: now + ms, serial: serial++, fn, repeat, canceled: false};
    events.push(event); return event;
  }
  const document = {hidden: false, addEventListener(event, fn) {
    if (!listeners.has(event)) listeners.set(event, []);
    listeners.get(event).push(fn);
  }};
  function element(id) {
    if (!elements.has(id)) {
      let text = '';
      const value = {hidden: false, disabled: false, value: 'recent', open: false, addEventListener() {}};
      Object.defineProperty(value, 'textContent', {get: () => text, set: next => {
        text = next; result.messages.push({id, text: next, at: now});
      }});
      elements.set(id, value);
    }
    return elements.get(id);
  }
  const context = vm.createContext({
    Map, Promise, AbortController, URLSearchParams, document,
    currentView: kind === 'favorites' ? 'favorites' : 'fan-owners',
    favorites: [{id: '123', name: 'Listener'}],
    favoriteActivity: new Map([['previous', {id: 'previous'}]]),
    favoriteActivitySeq: 0, favoriteActivityAbort: undefined,
    fanOwner: {id: '999', name: 'Owner'}, fanSeq: 0, fanAbort: undefined,
    fanOffset: 0, fanQuery: '', fanActivity: 'all', fanImportBusy: false,
    $: element, date: value => value, identity: user => 'ID ' + user.id,
    refreshFavoriteDestinations() {},
    renderFavorites() {result.rendered++; result.ids.push([...context.favoriteActivity.keys()]);},
    renderFanList(data) {result.rendered++; result.ids.push(data.fans.map(user => user.id));},
    setTimeout: (fn, ms) => schedule(fn, ms),
    clearTimeout: event => {event.canceled = true;},
    setInterval: (fn, ms) => schedule(fn, ms, ms),
    api(path, signal) {
      const requestIndex = result.started++;
      const delay = delays[requestIndex] ?? delays.at(-1);
      return new Promise((resolve, reject) => {
        let settled = false;
        const response = schedule(() => {
          if (settled) return;
          settled = true; result.completed++;
          const params = new URL('http://localhost' + path).searchParams;
          const ids = kind === 'favorites' ? params.get('ids').split(',') :
            [params.get('owner_id') + ':' + params.get('offset') + ':' + params.get('q')];
          const users = ids.map(id => ({id, name: 'Listener', recent: true,
            last_live_at: '2026-10-10T15:42:00Z'}));
          resolve(kind === 'favorites' ? {users, checked_at: 'CHECK-' + now} : {
            fans: users, total: 1, registered_count: 1, owner: {id: params.get('owner_id')},
            has_more: false, current_seconds: 600, checked_at: 'CHECK-' + now
          });
        }, delay);
        signal.addEventListener('abort', () => {
          if (settled) return;
          settled = true; response.canceled = true; result.aborted++;
          const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
        });
      });
    }
  });
  const helper = source.find(item => item.startsWith('async function listActivityApi('));
  if (helper) vm.runInContext(helper, context);
  vm.runInContext(line(kind === 'favorites' ? 'async function refreshFavoriteActivity(' : 'async function loadFans('), context);
  if (kind === 'favorites') {
    vm.runInContext(source.find(item => item.startsWith('setInterval(') && item.includes("currentView==='favorites'")), context);
  } else {
    const visible = source.find(item => item.startsWith('function refreshVisibleFans('));
    if (visible) vm.runInContext(visible, context);
    const poll = source.find(item => item.startsWith('setInterval(refreshVisibleFans')) ||
      source.find(item => item.includes("$('fans-prev').addEventListener") && item.includes('setInterval('));
    vm.runInContext(poll, context);
  }
  async function flush() {for (let i = 0; i < 32; i++) await Promise.resolve();}
  async function advance(target) {
    while (true) {
      events.sort((a, b) => a.at - b.at || a.serial - b.serial);
      const next = events.find(event => !event.canceled && event.at <= target);
      if (!next) break;
      events.splice(events.indexOf(next), 1); now = next.at;
      if (next.repeat) schedule(next.fn, next.repeat, next.repeat);
      next.fn(); await flush();
    }
    now = target; await flush();
  }
  function trigger() {vm.runInContext(kind === 'favorites' ? 'refreshFavoriteActivity()' : 'loadFans()', context);}
  if (options.many) context.favorites = Array.from({length: 101}, (_, i) => ({id: String(1000 + i)}));
  trigger();
  return {context, result, advance, trigger, element, hide() {document.hidden = true;}, show() {
    document.hidden = false; for (const fn of listeners.get('visibilitychange') || []) fn();
  }};
}
(async () => {
  const output = {};
  for (const kind of ['favorites', 'fans']) {
    const slow = await browser(kind, [31000]); await slow.advance(180000);
    const fast = await browser(kind, [5000]); await fast.advance(180000);
    const resume = await browser(kind, [5000]); await resume.advance(10000); resume.hide();
    await resume.advance(61000); resume.show(); await resume.advance(67000);
    const timeout = await browser(kind, [Infinity, 5000]); await timeout.advance(65001);
    const inactive = await browser(kind, [5000]); await inactive.advance(10000);
    inactive.context.currentView = 'home'; await inactive.advance(61000);
    const manual = [];
    for (const change of kind === 'favorites' ? ['selection'] : ['owner', 'filter', 'page']) {
      const view = await browser(kind, [31000, 10000, 31000]); await view.advance(5000);
      if (change === 'selection') view.context.favorites = [{id: '456', name: 'New listener'}];
      if (change === 'owner') view.context.fanOwner = {id: '888', name: 'New owner'};
      if (change === 'filter') view.context.fanQuery = 'new-filter';
      if (change === 'page') view.context.fanOffset = 50;
      view.trigger(); await view.advance(65000);
      manual.push({change, ...view.result});
    }
    output[kind] = {slow: slow.result, fast: fast.result, resume: resume.result,
      timeout: timeout.result, inactive: inactive.result, manual,
      displayedStatus: timeout.element(kind === 'favorites' ? 'favorites-activity-status' : 'fans-summary').textContent};
  }
  const batch = await browser('favorites', [31000], {many: true}); await batch.advance(63000);
  const batchTimeout = await browser('favorites', [31000, Infinity, 5000], {many: true});
  await batchTimeout.advance(95000);
  const preserved = [...batchTimeout.context.favoriteActivity.keys()];
  await batchTimeout.advance(131000);
  output.batch = batch.result;
  output.batchTimeout = {...batchTimeout.result, preserved};
  console.log(JSON.stringify(output));
})().catch(error => {console.error(error); process.exitCode = 1;});
"""


@unittest.skipUnless(shutil.which('node'), 'Node.js is optional; browser polling checks require it')
class ListPollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which('node')
        version = subprocess.run([node, '-p', 'process.versions.node'], check=True,
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        if int(version.split('.')[0]) < 18:
            raise unittest.SkipTest('Optional browser polling checks require Node.js 18 or newer')
        source = Path(__file__).resolve().parents[1]/'spoondev/static/index.html'
        result = subprocess.run([node, '-e', NODE_HARNESS, str(source)],
                                check=True, capture_output=True, text=True, timeout=15)
        cls.results = json.loads(result.stdout)

    def test_slow_background_updates_complete_without_aborting(self):
        for kind in ('favorites', 'fans'):
            with self.subTest(kind=kind):
                slow = self.results[kind]['slow']
                self.assertEqual((slow['started'], slow['completed'], slow['aborted'], slow['rendered']), (4, 3, 0, 3))
                fast = self.results[kind]['fast']
                self.assertEqual((fast['started'], fast['completed'], fast['aborted'], fast['rendered']), (7, 6, 0, 6))

    def test_manual_changes_cancel_old_results_and_preserve_new_request(self):
        expected = {'selection': '456', 'owner': '888:0:', 'filter': '999:0:new-filter', 'page': '999:50:'}
        for kind in ('favorites', 'fans'):
            for result in self.results[kind]['manual']:
                with self.subTest(kind=kind, change=result['change']):
                    self.assertEqual(result['aborted'], 1)
                    self.assertEqual(result['rendered'], 2)
                    self.assertEqual(result['ids'], [[expected[result['change']]]] * 2)
                    self.assertFalse(any('タイムアウト' in row['text'] for row in result['messages']))

    def test_visibility_resume_refreshes_only_the_active_list(self):
        for kind in ('favorites', 'fans'):
            with self.subTest(kind=kind):
                self.assertEqual(self.results[kind]['resume']['rendered'], 2)
                self.assertEqual(self.results[kind]['inactive']['started'], 1)

    def test_timeout_releases_the_gate_and_next_poll_renders(self):
        for kind in ('favorites', 'fans'):
            with self.subTest(kind=kind):
                result = self.results[kind]['timeout']
                self.assertEqual((result['started'], result['completed'], result['aborted'], result['rendered']), (2, 1, 1, 1))
                self.assertTrue(any('タイムアウト' in row['text'] for row in result['messages']))
                self.assertIn('CHECK-65000', self.results[kind]['displayedStatus'])

    def test_favorite_batches_have_separate_timeouts_and_commit_together(self):
        result = self.results['batch']
        self.assertEqual((result['started'], result['completed'], result['aborted'], result['rendered']), (2, 2, 0, 1))
        self.assertEqual(len(result['ids'][0]), 101)
        retry = self.results['batchTimeout']
        self.assertEqual(retry['preserved'], ['previous'])
        self.assertEqual((retry['started'], retry['completed'], retry['aborted'], retry['rendered']), (4, 3, 1, 1))
        self.assertEqual(len(retry['ids'][0]), 101)


if __name__ == '__main__':
    unittest.main()
