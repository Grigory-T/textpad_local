#!/usr/bin/env python3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse
from html import escape
import importlib.util
import json
import os
import queue
import re
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PAD_FILE = os.path.join(BASE, 'pad.txt')
TABS_FILE = os.path.join(BASE, 'tabs.json')
TABS_BACKUP_FILE = os.path.join(BASE, 'tabs.json.bak')
TABS_DIR = os.path.join(BASE, 'tabs')
DELETED_DIR = os.path.join(BASE, 'deleted')
DEFAULT_TAB_ID = 'main'
DEFAULT_TAB_NAME = 'Main'
MAX_TAB_NAME_LEN = 40
EVENT_HEARTBEAT_SECONDS = 15
EVENT_WRITE_TIMEOUT_SECONDS = 20
CLIENT_QUEUE_SIZE = 8

_cfg = importlib.util.spec_from_file_location('config', os.path.join(BASE, 'config.py'))
_mod = importlib.util.module_from_spec(_cfg)
_cfg.loader.exec_module(_mod)
HOST = _mod.HOST
PORT = _mod.PORT
MIRROR_DIR = getattr(_mod, 'MIRROR_DIR', os.path.join(BASE, 'mirror'))
MIRROR_DIR_MODE = 0o777
MIRROR_FILE_MODE = 0o666
MIRROR_FORBIDDEN_CHARS = '<>:"/\\|?*'

tabs_lock = threading.RLock()
clients = {}
clients_lock = threading.Lock()
file_lock = threading.Lock()


class EventClient:
    def __init__(self):
        self.queue = queue.Queue(maxsize=CLIENT_QUEUE_SIZE)


def log_event(event, **fields):
    row = {
        'event': event,
        'ts': round(time.time(), 3),
        **fields,
    }
    print(json.dumps(row, ensure_ascii=True, sort_keys=True), flush=True)


def timestamp():
    return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, content):
    parent = os.path.dirname(path) or '.'
    tmp_path = os.path.join(parent, '.' + os.path.basename(path) + '.tmp')
    with file_lock:
        os.makedirs(parent, exist_ok=True)
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            fsync_dir(parent)
        except Exception:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise


def tab_file(tab_id):
    return os.path.join(TABS_DIR, tab_id + '.txt')


def clean_tab_name(name):
    name = ' '.join((name or '').split())
    return name[:MAX_TAB_NAME_LEN]


def is_untitled_name(name):
    return re.fullmatch(r'Untitled(?:\s+\d+)?', name or '') is not None


def normalize_tab_name(name, tabs=None):
    name = clean_tab_name(name)
    if not name or is_untitled_name(name):
        return 'Untitled'
    return name


def slugify(name):
    slug = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')
    return slug[:32] or 'tab'


def valid_tab_id(tab_id):
    return isinstance(tab_id, str) and re.fullmatch(r'[a-z0-9-]{1,64}', tab_id)


def read_legacy_pad():
    try:
        with open(PAD_FILE, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return ''


def write_tabs_file(data):
    content = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    atomic_write(TABS_FILE, content)
    atomic_write(TABS_BACKUP_FILE, content)


def create_default_tabs_unlocked():
    os.makedirs(TABS_DIR, exist_ok=True)
    main_path = tab_file(DEFAULT_TAB_ID)
    if not os.path.exists(main_path):
        atomic_write(main_path, read_legacy_pad())
    data = {'tabs': [{'id': DEFAULT_TAB_ID, 'name': DEFAULT_TAB_NAME}]}
    write_tabs_file(data)
    return data


def valid_tab(tab):
    return (
        isinstance(tab, dict)
        and valid_tab_id(tab.get('id'))
        and isinstance(tab.get('name'), str)
    )


def read_tabs_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('tabs file is not a JSON object')
    return data


def clean_tabs_data(data):
    changed = False
    tabs = [tab for tab in data.get('tabs', []) if valid_tab(tab)]
    if not tabs:
        return None, True

    clean_tabs = []
    seen = set()
    for tab in tabs:
        tab_id = tab['id']
        if tab_id in seen:
            changed = True
            continue
        seen.add(tab_id)
        name = normalize_tab_name(tab['name'], clean_tabs)
        if name != tab['name']:
            changed = True
        clean_tabs.append({'id': tab_id, 'name': name})
        if not os.path.exists(tab_file(tab_id)):
            atomic_write(tab_file(tab_id), '')

    return {'tabs': clean_tabs}, changed


def recover_tabs_from_files_unlocked():
    recovered = []
    if os.path.isdir(TABS_DIR):
        for filename in sorted(os.listdir(TABS_DIR)):
            if not filename.endswith('.txt'):
                continue
            tab_id = filename[:-4]
            if not valid_tab_id(tab_id):
                continue
            name = DEFAULT_TAB_NAME if tab_id == DEFAULT_TAB_ID else tab_id[:MAX_TAB_NAME_LEN]
            recovered.append({'id': tab_id, 'name': name})

    if not recovered:
        return None

    data = {'tabs': recovered}
    write_tabs_file(data)
    log_event('tabs_recovered_from_files', tabs=len(recovered))
    return data


def move_bad_tabs_file_unlocked():
    if not os.path.exists(TABS_FILE):
        return None
    bad_path = TABS_FILE + '.bad.' + timestamp()
    with file_lock:
        if not os.path.exists(TABS_FILE):
            return None
        os.replace(TABS_FILE, bad_path)
        fsync_dir(BASE)
    return bad_path


def restore_tabs_from_backup_unlocked():
    if not os.path.exists(TABS_BACKUP_FILE):
        return None
    try:
        data = read_tabs_json(TABS_BACKUP_FILE)
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    clean_data, changed = clean_tabs_data(data)
    if clean_data is None:
        return None

    write_tabs_file(clean_data)
    log_event('tabs_restored_from_backup', changed=changed)
    return clean_data


def load_tabs():
    with tabs_lock:
        os.makedirs(TABS_DIR, exist_ok=True)
        if not os.path.exists(TABS_FILE):
            recovered = recover_tabs_from_files_unlocked()
            return recovered if recovered else create_default_tabs_unlocked()

        try:
            data = read_tabs_json(TABS_FILE)
        except (OSError, json.JSONDecodeError, ValueError):
            bad_path = move_bad_tabs_file_unlocked()
            restored = restore_tabs_from_backup_unlocked()
            if restored:
                if bad_path:
                    log_event('tabs_bad_file_saved', path=bad_path)
                return restored
            recovered = recover_tabs_from_files_unlocked()
            if recovered:
                if bad_path:
                    log_event('tabs_bad_file_saved', path=bad_path)
                return recovered
            return create_default_tabs_unlocked()

        clean_data, changed = clean_tabs_data(data)
        if clean_data is None:
            restored = restore_tabs_from_backup_unlocked()
            if restored:
                return restored
            recovered = recover_tabs_from_files_unlocked()
            return recovered if recovered else create_default_tabs_unlocked()
        if changed:
            write_tabs_file(clean_data)
        return clean_data


def find_tab(data, tab_id):
    for tab in data['tabs']:
        if tab['id'] == tab_id:
            return tab
    return None


def requested_tab_id(parsed):
    values = parse_qs(parsed.query, keep_blank_values=True).get('tab')
    if not values:
        return None
    return values[0]


def select_tab(requested_tab_id=None):
    data = load_tabs()
    if requested_tab_id and find_tab(data, requested_tab_id):
        return data, requested_tab_id
    return data, data['tabs'][0]['id']


def read_pad(tab_id):
    try:
        with open(tab_file(tab_id), 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return ''


def tab_revision(tab_id):
    try:
        return os.stat(tab_file(tab_id)).st_mtime_ns
    except FileNotFoundError:
        return 0


def all_revisions(data):
    return {tab['id']: tab_revision(tab['id']) for tab in data['tabs']}


def mirror_failure(action, error):
    log_event(
        'mirror_failed',
        action=action,
        error=error.__class__.__name__,
        message=str(error)[:200],
    )


def mirror_safe_name(name):
    value = ' '.join((name or '').split()) or 'Untitled'
    clean = []
    for char in value:
        if char in MIRROR_FORBIDDEN_CHARS or ord(char) < 32:
            clean.append('_')
        else:
            clean.append(char)
    value = ''.join(clean).strip(' .')
    return (value or 'Untitled')[:80]


def mirror_filename(index, tab):
    return f'{index}. {mirror_safe_name(tab["name"])}.txt'


def ensure_mirror_dir():
    if not MIRROR_DIR:
        return False
    os.makedirs(MIRROR_DIR, exist_ok=True)
    os.chmod(MIRROR_DIR, MIRROR_DIR_MODE)
    return True


def mirror_write_file(filename, text):
    path = os.path.join(MIRROR_DIR, filename)
    atomic_write(path, text)
    os.chmod(path, MIRROR_FILE_MODE)


def mirror_tab(data, tab_id, text=None):
    if not MIRROR_DIR:
        return
    try:
        if not ensure_mirror_dir():
            return
        for index, tab in enumerate(data['tabs'], 1):
            if tab['id'] == tab_id:
                mirror_write_file(mirror_filename(index, tab), read_pad(tab_id) if text is None else text)
                return
    except Exception as error:
        mirror_failure('tab', error)


def mirror_all_tabs(data=None):
    if not MIRROR_DIR:
        return
    try:
        if not ensure_mirror_dir():
            return
        if data is None:
            data = load_tabs()
        expected = set()
        for index, tab in enumerate(data['tabs'], 1):
            filename = mirror_filename(index, tab)
            expected.add(filename)
            mirror_write_file(filename, read_pad(tab['id']))

        for filename in os.listdir(MIRROR_DIR):
            path = os.path.join(MIRROR_DIR, filename)
            if filename not in expected and filename.endswith('.txt') and os.path.isfile(path):
                os.unlink(path)
        fsync_dir(MIRROR_DIR)
    except Exception as error:
        mirror_failure('all', error)


def write_pad(tab_id, text):
    with tabs_lock:
        data = load_tabs()
        if not find_tab(data, tab_id):
            return False, 0
        atomic_write(tab_file(tab_id), text)
        mirror_tab(data, tab_id, text)
        return True, tab_revision(tab_id)


def all_contents(data):
    return {tab['id']: read_pad(tab['id']) for tab in data['tabs']}


def unique_tab_id(tabs, name, requested_id=None):
    existing = {tab['id'] for tab in tabs}
    base = requested_id if valid_tab_id(requested_id) else slugify(name)
    tab_id = base
    n = 2
    while tab_id in existing:
        suffix = '-' + str(n)
        tab_id = base[:64 - len(suffix)] + suffix
        n += 1
    return tab_id


def create_tab(name, requested_id=None, after_id=None):
    with tabs_lock:
        data = load_tabs()
        name = normalize_tab_name(name, data['tabs'])
        tab_id = unique_tab_id(data['tabs'], name, requested_id)
        new_tab = {'id': tab_id, 'name': name}
        insert_at = len(data['tabs'])
        for i, tab in enumerate(data['tabs']):
            if tab['id'] == after_id:
                insert_at = i + 1
                break
        data['tabs'].insert(insert_at, new_tab)
        atomic_write(tab_file(tab_id), '')
        write_tabs_file(data)
        mirror_all_tabs(data)
        return tab_id, data


def rename_tab(tab_id, name):
    with tabs_lock:
        data = load_tabs()
        for tab in data['tabs']:
            if tab['id'] == tab_id:
                others = [item for item in data['tabs'] if item['id'] != tab_id]
                name = normalize_tab_name(name, others)
                tab['name'] = name
                write_tabs_file(data)
                mirror_all_tabs(data)
                return True, data
        return False, data


def delete_tab(tab_id):
    with tabs_lock:
        data = load_tabs()
        tabs = data['tabs']
        if len(tabs) <= 1:
            return tabs[0]['id'], data

        delete_index = None
        for i, tab in enumerate(tabs):
            if tab['id'] == tab_id:
                delete_index = i
                break
        if delete_index is None:
            return tabs[0]['id'], data

        new_tabs = tabs[:delete_index] + tabs[delete_index + 1:]
        next_tab_id = new_tabs[min(delete_index, len(new_tabs) - 1)]['id']
        data = {'tabs': new_tabs}
        write_tabs_file(data)
        archived_path = archive_deleted_tab(tab_id)
        with clients_lock:
            clients.pop(tab_id, None)
        if archived_path:
            log_event('tab_archived_on_delete', tab=tab_id, path=archived_path)
        mirror_all_tabs(data)
        return next_tab_id, data


def archive_deleted_tab(tab_id):
    src = tab_file(tab_id)
    base_name = timestamp() + '-' + tab_id + '.txt'
    dst = os.path.join(DELETED_DIR, base_name)
    n = 2
    while os.path.exists(dst):
        dst = os.path.join(DELETED_DIR, timestamp() + '-' + tab_id + '-' + str(n) + '.txt')
        n += 1
    with file_lock:
        if not os.path.exists(src):
            return None
        os.makedirs(DELETED_DIR, exist_ok=True)
        os.replace(src, dst)
        fsync_dir(TABS_DIR)
        fsync_dir(DELETED_DIR)
    return dst


def event_frame(content, revision):
    return (
        'id: ' + str(revision) + '\n'
        + 'data: ' + json.dumps(content) + '\n\n'
    ).encode()


def add_client(tab_id):
    client = EventClient()
    with clients_lock:
        clients.setdefault(tab_id, []).append(client)
    return client


def remove_client(tab_id, client):
    with clients_lock:
        tab_clients = clients.get(tab_id, [])
        if client in tab_clients:
            tab_clients.remove(client)
        if not tab_clients and tab_id in clients:
            del clients[tab_id]


def enqueue_client(client, content, revision):
    item = (content, revision)
    try:
        client.queue.put_nowait(item)
        return
    except queue.Full:
        pass

    try:
        while True:
            client.queue.get_nowait()
    except queue.Empty:
        pass

    try:
        client.queue.put_nowait(item)
    except queue.Full:
        pass


def broadcast(tab_id, content, revision=None):
    if revision is None:
        revision = tab_revision(tab_id)
    with clients_lock:
        tab_clients = list(clients.get(tab_id, []))
    for client in tab_clients:
        enqueue_client(client, content, revision)


def page_html(data, active_tab_id):
    payload = {
        'tabs': data['tabs'],
        'active': active_tab_id,
        'contents': all_contents(data),
        'revisions': all_revisions(data),
    }
    payload_json = (
        json.dumps(payload, ensure_ascii=False)
        .replace('&', '\\u0026')
        .replace('<', '\\u003c')
        .replace('>', '\\u003e')
    )
    html = '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>textpad_local</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #1a1a1a; color: #ddd; font-family: monospace; display: flex; flex-direction: column; height: 100vh; padding: 8px; gap: 6px; }
#tabs { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.tab { color: #aaa; background: #111; border: 1px solid #444; padding: 3px 8px; min-height: 24px; cursor: pointer; font-family: monospace; }
.tab.active { color: #fff; background: #555; border-color: #ddd; font-weight: bold; }
#tab-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.action-block { display: flex; gap: 4px; align-items: center; padding: 5px 7px; border: 1px solid #333; background: #151515; }
#delete-tab { margin-left: 10px; border-color: #4a2a2a; background: #181111; }
#new-tab, #rename-tab, #delete-tab { display: flex; gap: 4px; align-items: center; }
#new-tab input, #rename-tab input { width: 130px; background: #111; color: #ddd; border: 1px solid #444; padding: 3px 6px; font-family: monospace; }
button { font-size: 12px; color: #aaa; background: #111; border: 1px solid #444; padding: 3px 8px; cursor: pointer; font-family: monospace; }
button:hover { color: #ddd; border-color: #777; }
button:disabled { color: #555; cursor: default; border-color: #333; }
#delete-tab button:hover { color: #c66; border-color: #c66; }
#toolbar { display: flex; align-items: center; gap: 16px; }
#title { font-size: 20px; color: #888; letter-spacing: 0.08em; font-weight: bold; }
#pos { font-size: 13px; color: #666; }
#status { font-size: 13px; color: #6a6; opacity: 0; transition: opacity 0.2s; }
#clear { color: #666; }
#clear:hover { color: #c66; border-color: #c66; }
#editor { display: flex; flex: 1; border: 1px solid #444; overflow: hidden; background: #111; }
#lines { padding: 8px 6px 8px 8px; background: #1e1e1e; color: #555; font-size: 14px; line-height: 1.5; text-align: right; user-select: none; overflow: hidden; white-space: pre; min-width: 2.5em; border-right: 1px solid #333; }
textarea { flex: 1; background: #111; color: #ddd; border: none; padding: 8px; font-size: 14px; font-family: monospace; line-height: 1.5; resize: none; outline: none; white-space: pre; overflow-x: auto; wrap: off; tab-size: 4; }
</style>
</head>
<body>
<div id="tabs"></div>
<div id="tab-actions">
  <form id="new-tab" class="action-block">
    <input name="name" maxlength="40" placeholder="new tab">
    <button type="submit">add tab</button>
  </form>
  <form id="rename-tab" class="action-block">
    <input name="name" maxlength="40">
    <button type="submit">rename tab</button>
  </form>
  <form id="delete-tab" class="action-block">
    <button type="submit">delete tab</button>
  </form>
</div>
<div id="toolbar">
  <span id="title">textpad_local</span>
  <span id="pos">Ln 1, Col 1</span>
  <span id="status">Saved</span>
  <button id="add-row" type="button">add row below</button>
  <button id="clear">clear all</button>
</div>
<div id="editor">
  <div id="lines">1</div>
  <textarea id="text" autofocus wrap="off" spellcheck="false" autocorrect="off" autocapitalize="off" autocomplete="off"></textarea>
</div>
<script id="initial-state" type="application/json">''' + payload_json + '''</script>
<script>
const initial = JSON.parse(document.getElementById('initial-state').textContent);
let tabs = initial.tabs;
let contents = initial.contents;
let revisions = initial.revisions || {};
let activeTab = initial.active;
let timer = null;
let dirty = false;
let lastTyped = 0;
let statusTimer = null;
let es = null;
let pendingRemote = null;
let refreshInFlight = false;
let lastSyncAt = Date.now();
const DRAFT_PREFIX = 'textpad_local:draft:';
const REMOTE_APPLY_IDLE_MS = 1000;
const REFRESH_INTERVAL_MS = 5000;

const tabsEl = document.getElementById('tabs');
const ta = document.getElementById('text');
const ln = document.getElementById('lines');
const status = document.getElementById('status');
const pos = document.getElementById('pos');
const renameInput = document.querySelector('#rename-tab input[name="name"]');
const deleteButton = document.querySelector('#delete-tab button');

function clientLog(event, fields) {
  const data = new URLSearchParams(Object.assign({
    event: event,
    tab: activeTab || '',
    url: location.pathname + location.search
  }, fields || {})).toString();
  if (navigator.sendBeacon) {
    navigator.sendBeacon('/client-log', new Blob([data], {type: 'application/x-www-form-urlencoded'}));
  } else {
    fetch('/client-log', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: data,
      keepalive: true
    }).catch(() => {});
  }
}
function logDuration(event, started, fields) {
  clientLog(event, Object.assign({ms: Math.round(performance.now() - started)}, fields || {}));
}
function draftKey(tabId) {
  return DRAFT_PREFIX + tabId;
}
function storeDraft(tabId, text) {
  if (!tabId) return;
  const started = performance.now();
  try {
    localStorage.setItem(draftKey(tabId), JSON.stringify({text: text, ts: Date.now()}));
    const ms = performance.now() - started;
    if (ms > 200) clientLog('draft_store_slow', {ms: Math.round(ms)});
  } catch (err) {
    clientLog('draft_store_failed', {error: (err && err.name) || 'error'});
  }
}
function loadDraft(tabId) {
  if (!tabId) return null;
  try {
    const raw = localStorage.getItem(draftKey(tabId));
    if (!raw) return null;
    const draft = JSON.parse(raw);
    return typeof draft.text === 'string' ? draft : null;
  } catch (err) {
    return null;
  }
}
function clearDraft(tabId) {
  if (!tabId) return;
  try {
    localStorage.removeItem(draftKey(tabId));
  } catch (err) {}
}
function currentRevision(tabId) {
  return Number(revisions[tabId] || 0);
}
function updateRevision(tabId, revision) {
  const value = Number(revision || 0);
  if (value && value >= currentRevision(tabId)) revisions[tabId] = value;
}
function canApplyRemote() {
  return !dirty && Date.now() - lastTyped >= REMOTE_APPLY_IDLE_MS;
}
function schedulePendingRemote() {
  const wait = Math.max(200, REMOTE_APPLY_IDLE_MS - (Date.now() - lastTyped) + 50);
  setTimeout(applyPendingRemote, wait);
}
function applyRemoteText(text, revision) {
  const incomingRevision = Number(revision || 0);
  const knownRevision = currentRevision(activeTab);
  if (incomingRevision && incomingRevision < knownRevision) return;
  if (incomingRevision && incomingRevision === knownRevision && text === contents[activeTab]) {
    lastSyncAt = Date.now();
    return;
  }
  if (!canApplyRemote()) {
    pendingRemote = {text: text, revision: incomingRevision};
    schedulePendingRemote();
    return;
  }
  pendingRemote = null;
  contents[activeTab] = text;
  updateRevision(activeTab, incomingRevision);
  ta.value = text;
  clearDraft(activeTab);
  updateLines();
  updatePos();
  ln.scrollTop = ta.scrollTop;
  lastSyncAt = Date.now();
}
function applyPendingRemote() {
  if (!pendingRemote) return;
  if (!canApplyRemote()) {
    schedulePendingRemote();
    return;
  }
  const remote = pendingRemote;
  if (remote.revision && remote.revision < currentRevision(activeTab)) {
    pendingRemote = null;
    return;
  }
  applyRemoteText(remote.text, remote.revision);
}
function scheduleSave(delay, tabId) {
  if (timer) clearTimeout(timer);
  const saveTab = tabId || activeTab;
  timer = setTimeout(() => {
    if (saveTab === activeTab) saveNow(false);
  }, delay);
}
function applyDraft(tabId) {
  const draft = loadDraft(tabId);
  if (!draft) return;
  const serverText = contents[tabId] || '';
  if (draft.text === serverText) {
    clearDraft(tabId);
    return;
  }
  if (tabId !== activeTab) return;
  contents[tabId] = draft.text;
  ta.value = draft.text;
  dirty = true;
  showStatus('Recovered draft', true);
  scheduleSave(100, tabId);
}
let lagTick = performance.now();
setInterval(() => {
  const now = performance.now();
  const lag = now - lagTick - 1000;
  lagTick = now;
  if (lag > 1500) clientLog('client_lag', {lag_ms: Math.round(lag)});
}, 1000);

function tabById(id) {
  return tabs.find((tab) => tab.id === id);
}
function cleanName(name) {
  const value = (name || '').trim().replace(/\\s+/g, ' ');
  return value.slice(0, 40);
}
function isUntitledName(name) {
  return /^Untitled(?:\\s+\\d+)?$/.test(name || '');
}
function tabName(name, excludeId) {
  const value = cleanName(name);
  return value && !isUntitledName(value) ? value : 'Untitled';
}
function slugify(name) {
  const slug = (name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 32);
  return slug || 'tab';
}
function uniqueId(name) {
  const existing = new Set(tabs.map((tab) => tab.id));
  const base = slugify(name);
  let id = base;
  let n = 2;
  while (existing.has(id)) {
    const suffix = '-' + n;
    id = base.slice(0, 64 - suffix.length) + suffix;
    n++;
  }
  return id;
}
function showStatus(text, ok) {
  if (statusTimer) clearTimeout(statusTimer);
  status.textContent = text;
  status.style.color = ok ? '#6a6' : '#c66';
  status.style.opacity = '1';
  statusTimer = setTimeout(() => status.style.opacity = '0', 900);
}
function updateLines() {
  const count = ta.value.split('\\n').length;
  ln.textContent = Array.from({length: count}, (_, i) => i + 1).join('\\n');
}
function syncScroll() {
  ln.scrollTop = ta.scrollTop;
}
function updatePos() {
  const before = ta.value.slice(0, ta.selectionStart);
  const line = before.split('\\n').length;
  const col = before.split('\\n').pop().length + 1;
  pos.textContent = 'Ln ' + line + ', Col ' + col;
}
function lineForOffset(value, offset) {
  return value.slice(0, offset).split('\\n').length - 1;
}
function scrollLineIntoView(line) {
  const style = window.getComputedStyle(ta);
  const lineHeight = parseFloat(style.lineHeight) || 21;
  const target = Math.max(0, line * lineHeight - ta.clientHeight * 0.2);
  ta.scrollTop = target;
  ln.scrollTop = ta.scrollTop;
}
function resetEditorToTop() {
  ta.setSelectionRange(0, 0);
  ta.scrollTop = 0;
  ta.scrollLeft = 0;
  ln.scrollTop = 0;
  updatePos();
  requestAnimationFrame(() => {
    ta.scrollTop = 0;
    ta.scrollLeft = 0;
    ln.scrollTop = 0;
  });
}
function editIndentation(value, start, end, outdent) {
  if (start === end && !outdent) {
    return {value: value.slice(0, start) + '\\t' + value.slice(end), start: start + 1, end: start + 1};
  }

  const lineStart = value.lastIndexOf('\\n', Math.max(0, start - 1)) + 1;
  const selectionEndsAtNextLine = end > start && value[end - 1] === '\\n';
  const effectiveEnd = selectionEndsAtNextLine ? end - 1 : end;
  const nextNewline = value.indexOf('\\n', effectiveEnd);
  const blockEnd = nextNewline === -1 ? value.length : nextNewline;
  const block = value.slice(lineStart, blockEnd);
  const lines = block.split('\\n');
  const edits = [];
  let offset = lineStart;

  const changed = lines.map((line) => {
    let remove = 0;
    let insert = '';
    if (outdent) {
      if (line.startsWith('\\t')) remove = 1;
      else {
        const spaces = line.match(/^ {1,4}/);
        if (spaces) remove = spaces[0].length;
      }
    } else {
      insert = '\\t';
    }
    edits.push({position: offset, remove: remove, insert: insert.length});
    offset += line.length + 1;
    return insert + line.slice(remove);
  }).join('\\n');

  function mapOffset(original) {
    let mapped = original;
    edits.forEach((edit) => {
      if (original <= edit.position) return;
      if (original <= edit.position + edit.remove) {
        mapped = edit.position + edit.insert;
      } else {
        mapped += edit.insert - edit.remove;
      }
    });
    return mapped;
  }

  return {
    value: value.slice(0, lineStart) + changed + value.slice(blockEnd),
    start: mapOffset(start),
    end: mapOffset(end)
  };
}
function renderTabs() {
  tabsEl.textContent = '';
  tabs.forEach((tab, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = tab.id === activeTab ? 'tab active' : 'tab';
    button.textContent = (index + 1) + '. ' + tab.name;
    button.addEventListener('click', () => selectTab(tab.id));
    tabsEl.appendChild(button);
  });
  const active = tabById(activeTab);
  renameInput.value = active ? active.name : '';
  deleteButton.disabled = tabs.length <= 1;
}
function setUrl() {
  history.replaceState(null, '', '/?tab=' + encodeURIComponent(activeTab));
}
function loadActiveText() {
  ta.value = contents[activeTab] || '';
  dirty = false;
  applyDraft(activeTab);
  updateLines();
  resetEditorToTop();
}
function connectEvents() {
  if (es) es.close();
  es = new EventSource('/events?tab=' + encodeURIComponent(activeTab));
  es.onopen = () => {
    lastSyncAt = Date.now();
  };
  es.onmessage = (e) => {
    const incoming = JSON.parse(e.data);
    applyRemoteText(incoming, e.lastEventId);
  };
  es.onerror = () => {
    refreshFromServer(false).catch(() => {});
  };
}
function postForm(path, fields) {
  const body = new URLSearchParams(fields);
  return fetch(path, {method: 'POST', body: body, headers: {'Accept': 'application/json'}})
    .then((res) => {
      if (!res.ok) throw new Error('request failed');
      return res.json();
    });
}
function saveNow(background) {
  const started = performance.now();
  const tabId = activeTab;
  const text = ta.value;
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
  if (!dirty && contents[tabId] === text && !loadDraft(tabId)) return Promise.resolve();
  contents[tabId] = text;
  if (tabId === activeTab) dirty = false;
  storeDraft(tabId, text);
  const body = new URLSearchParams({text: text}).toString();
  const url = '/?tab=' + encodeURIComponent(tabId);
  if (background && navigator.sendBeacon) {
    const queued = navigator.sendBeacon(url, new Blob([body], {type: 'application/x-www-form-urlencoded'}));
    if (!queued && tabId === activeTab) {
      dirty = true;
      scheduleSave(1000, tabId);
    }
    return Promise.resolve();
  }
  return fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: body,
    keepalive: !!background
  }).then((res) => {
    if (!res.ok) throw new Error('save failed');
    const revision = Number(res.headers.get('X-Textpad-Revision') || 0);
    updateRevision(tabId, revision);
    if (pendingRemote && pendingRemote.revision && pendingRemote.revision <= currentRevision(tabId)) {
      pendingRemote = null;
    }
    const ms = performance.now() - started;
    if (ms > 1000) clientLog('save_slow', {ms: Math.round(ms), background: background ? '1' : '0'});
    const draft = loadDraft(tabId);
    if (draft && draft.text === text) clearDraft(tabId);
    if (tabId === activeTab && contents[tabId] === text) {
      dirty = false;
      if (!background) showStatus('Saved', true);
    }
    applyPendingRemote();
  }).catch((err) => {
    if (tabId === activeTab) {
      dirty = true;
      showStatus('Save failed', false);
      scheduleSave(1500, tabId);
    }
    logDuration('save_failed', started, {background: background ? '1' : '0'});
  });
}
function selectTab(id) {
  const started = performance.now();
  if (id === activeTab || !tabById(id)) return;
  saveNow(true);
  pendingRemote = null;
  activeTab = id;
  renderTabs();
  loadActiveText();
  setUrl();
  connectEvents();
  ta.focus();
  logDuration('select_tab', started, {to: id});
}
function refreshFromServer(force) {
  if (refreshInFlight) return Promise.resolve();
  if (!force && !canApplyRemote()) return Promise.resolve();
  refreshInFlight = true;
  return fetch('/state?tab=' + encodeURIComponent(activeTab), {headers: {'Accept': 'application/json'}})
    .then((res) => res.json())
    .then((state) => {
      const previousActive = activeTab;
      tabs = state.tabs;
      revisions = Object.assign({}, revisions, state.revisions || {});
      Object.keys(state.contents || {}).forEach((tabId) => {
        if (tabId !== previousActive) contents[tabId] = state.contents[tabId];
      });
      if (!tabById(previousActive)) activeTab = state.active;
      const activeChanged = activeTab !== previousActive;
      if (activeTab === previousActive) {
        applyRemoteText(state.contents[activeTab] || '', currentRevision(activeTab));
      } else {
        contents[activeTab] = state.contents[activeTab] || '';
        pendingRemote = null;
        loadActiveText();
      }
      renderTabs();
      setUrl();
      if (activeChanged) connectEvents();
      lastSyncAt = Date.now();
    })
    .finally(() => {
      refreshInFlight = false;
    });
}

ta.addEventListener('input', () => {
  dirty = true;
  lastTyped = Date.now();
  pendingRemote = null;
  contents[activeTab] = ta.value;
  storeDraft(activeTab, ta.value);
  updateLines();
  updatePos();
  scheduleSave(300, activeTab);
});
ta.addEventListener('keydown', (event) => {
  if (event.key !== 'Tab') return;
  event.preventDefault();
  const direction = ta.selectionDirection;
  const edit = editIndentation(ta.value, ta.selectionStart, ta.selectionEnd, event.shiftKey);
  if (edit.value === ta.value) return;
  ta.value = edit.value;
  ta.setSelectionRange(edit.start, edit.end, direction);
  ta.dispatchEvent(new Event('input', {bubbles: true}));
});
ta.addEventListener('keyup', updatePos);
ta.addEventListener('mouseup', updatePos);
ta.addEventListener('click', updatePos);
ta.addEventListener('scroll', syncScroll);
ta.addEventListener('paste', () => {
  const pasteStart = ta.selectionStart;
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      updateLines();
      updatePos();
      scrollLineIntoView(lineForOffset(ta.value, pasteStart));
    });
  });
});
window.addEventListener('beforeunload', () => saveNow(true));
window.addEventListener('pagehide', () => saveNow(true));
window.addEventListener('pageshow', resetEditorToTop);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    saveNow(true);
  } else {
    refreshFromServer(false).catch(() => {});
  }
});
window.addEventListener('focus', () => refreshFromServer(false).catch(() => {}));
setInterval(() => {
  if (document.visibilityState === 'visible' && Date.now() - lastSyncAt > REFRESH_INTERVAL_MS) {
    refreshFromServer(false).catch(() => {});
  }
}, REFRESH_INTERVAL_MS);

document.getElementById('clear').addEventListener('click', () => {
  ta.value = '';
  dirty = true;
  contents[activeTab] = '';
  storeDraft(activeTab, ta.value);
  updateLines();
  updatePos();
  saveNow(false);
});
document.getElementById('add-row').addEventListener('click', () => {
  ta.value += '\\n';
  const end = ta.value.length;
  ta.setSelectionRange(end, end);
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  ta.focus();
  requestAnimationFrame(() => {
    ta.scrollTop = ta.scrollHeight;
    ta.scrollLeft = 0;
    ln.scrollTop = ta.scrollTop;
  });
});
document.getElementById('new-tab').addEventListener('submit', (e) => {
  e.preventDefault();
  const started = performance.now();
  saveNow(true);
  const input = e.currentTarget.querySelector('input[name="name"]');
  const after = activeTab;
  const name = tabName(input.value);
  const id = uniqueId(name);
  const index = tabs.findIndex((tab) => tab.id === after);
  const insertAt = index >= 0 ? index + 1 : tabs.length;
  tabs.splice(insertAt, 0, {id: id, name: name});
  contents[id] = '';
  input.value = '';
  activeTab = id;
  ta.value = '';
  dirty = false;
  renderTabs();
  updateLines();
  updatePos();
  setUrl();
  connectEvents();
  ta.focus();
  logDuration('create_tab_local', started, {new_tab: id});
  postForm('/tabs', {json: '1', action: 'create', id: id, name: name, after: after})
    .then((state) => {
      tabs = state.tabs;
      contents = state.contents;
      revisions = state.revisions || revisions;
      activeTab = state.active;
      renderTabs();
      loadActiveText();
      setUrl();
      logDuration('create_tab_server', started, {new_tab: activeTab});
    })
    .catch(() => { logDuration('create_tab_failed', started, {new_tab: id}); showStatus('Add failed', false); refreshFromServer(); });
});
document.getElementById('rename-tab').addEventListener('submit', (e) => {
  e.preventDefault();
  const started = performance.now();
  saveNow(true);
  const name = tabName(renameInput.value, activeTab);
  const tab = tabById(activeTab);
  if (!tab) return;
  tab.name = name;
  renderTabs();
  logDuration('rename_tab_local', started, {name: name});
  postForm('/tabs', {json: '1', action: 'rename', tab: activeTab, name: name})
    .then((state) => {
      tabs = state.tabs;
      contents = state.contents;
      revisions = state.revisions || revisions;
      activeTab = state.active;
      renderTabs();
      setUrl();
      logDuration('rename_tab_server', started, {name: name});
    })
    .catch(() => { logDuration('rename_tab_failed', started, {name: name}); showStatus('Rename failed', false); refreshFromServer(); });
});
document.getElementById('delete-tab').addEventListener('submit', (e) => {
  e.preventDefault();
  const started = performance.now();
  if (tabs.length <= 1) return;
  const deleted = activeTab;
  const index = tabs.findIndex((tab) => tab.id === deleted);
  const next = tabs[Math.min(index + 1, tabs.length - 1)] || tabs[index - 1];
  tabs = tabs.filter((tab) => tab.id !== deleted);
  delete contents[deleted];
  clearDraft(deleted);
  activeTab = next.id === deleted ? tabs[0].id : next.id;
  renderTabs();
  loadActiveText();
  setUrl();
  connectEvents();
  ta.focus();
  logDuration('delete_tab_local', started, {deleted: deleted, next: activeTab});
  postForm('/tabs', {json: '1', action: 'delete', tab: deleted})
    .then((state) => {
      tabs = state.tabs;
      contents = state.contents;
      revisions = state.revisions || revisions;
      activeTab = state.active;
      renderTabs();
      loadActiveText();
      setUrl();
      connectEvents();
      logDuration('delete_tab_server', started, {deleted: deleted, next: activeTab});
    })
    .catch(() => { logDuration('delete_tab_failed', started, {deleted: deleted}); showStatus('Delete failed', false); refreshFromServer(); });
});

renderTabs();
loadActiveText();
setUrl();
connectEvents();
ta.focus();
clientLog('page_ready', {tabs: String(tabs.length)});
</script>
</body>
</html>'''
    return html.encode('utf-8')


class PadHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _ip(self):
        return self.client_address[0] if self.client_address else ''

    def _send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _not_found(self):
        body = b'not found\n'
        self.send_response(404)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _state_payload(self, data, active_tab_id):
        return {
            'tabs': data['tabs'],
            'active': active_tab_id,
            'contents': all_contents(data),
            'revisions': all_revisions(data),
        }

    def do_GET(self):
        started = time.monotonic()
        parsed = urlparse(self.path)
        data, active_tab_id = select_tab(requested_tab_id(parsed))

        if parsed.path == '/state':
            self._send_json(self._state_payload(data, active_tab_id))
            log_event(
                'request',
                method='GET',
                path=parsed.path,
                tab=active_tab_id,
                ip=self._ip(),
                ms=round((time.monotonic() - started) * 1000, 1),
            )
            return

        if parsed.path == '/events':
            tab_id = active_tab_id
            self.connection.settimeout(EVENT_WRITE_TIMEOUT_SECONDS)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            client = add_client(tab_id)
            try:
                self.wfile.write(event_frame(read_pad(tab_id), tab_revision(tab_id)))
                self.wfile.flush()
                while True:
                    try:
                        content, revision = client.queue.get(timeout=EVENT_HEARTBEAT_SECONDS)
                        self.wfile.write(event_frame(content, revision))
                    except queue.Empty:
                        self.wfile.write(b': keep-alive\n\n')
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                remove_client(tab_id, client)
                log_event(
                    'events_closed',
                    tab=tab_id,
                    ip=self._ip(),
                    ms=round((time.monotonic() - started) * 1000, 1),
                )
            return

        if parsed.path != '/':
            self._not_found()
            log_event(
                'request',
                method='GET',
                path=parsed.path,
                status=404,
                ip=self._ip(),
                ms=round((time.monotonic() - started) * 1000, 1),
            )
            return

        body = page_html(data, active_tab_id)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
        log_event(
            'request',
            method='GET',
            path=parsed.path,
            tab=active_tab_id,
            ip=self._ip(),
            bytes=len(body),
            ms=round((time.monotonic() - started) * 1000, 1),
        )

    def do_HEAD(self):
        started = time.monotonic()
        parsed = urlparse(self.path)
        if parsed.path != '/':
            self._not_found()
            log_event(
                'request',
                method='HEAD',
                path=parsed.path,
                status=404,
                ip=self._ip(),
                ms=round((time.monotonic() - started) * 1000, 1),
            )
            return
        data, active_tab_id = select_tab(requested_tab_id(parsed))
        body = page_html(data, active_tab_id)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        log_event(
            'request',
            method='HEAD',
            path=parsed.path,
            tab=active_tab_id,
            ip=self._ip(),
            bytes=len(body),
            ms=round((time.monotonic() - started) * 1000, 1),
        )

    def do_POST(self):
        started = time.monotonic()
        parsed = urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8')
        fields = parse_qs(body, keep_blank_values=True)

        if parsed.path == '/client-log':
            clean = {
                key: values[0][:200]
                for key, values in fields.items()
                if values and re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', key)
            }
            client_event = clean.pop('event', '')
            log_event('client', ip=self._ip(), client_event=client_event, **clean)
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        if parsed.path == '/tabs':
            action = fields.get('action', [''])[0]
            json_mode = fields.get('json', [''])[0] == '1'

            if action == 'create':
                tab_id, data = create_tab(
                    fields.get('name', [''])[0],
                    fields.get('id', [None])[0],
                    fields.get('after', [None])[0],
                )
                if json_mode:
                    self._send_json(self._state_payload(data, tab_id))
                else:
                    self._redirect('/?tab=' + quote(tab_id))
                log_event(
                    'request',
                    method='POST',
                    path=parsed.path,
                    action=action,
                    tab=tab_id,
                    ip=self._ip(),
                    ms=round((time.monotonic() - started) * 1000, 1),
                )
                return

            if action == 'rename':
                tab_id = fields.get('tab', [''])[0]
                ok, data = rename_tab(tab_id, fields.get('name', [''])[0])
                active = tab_id if ok else data['tabs'][0]['id']
                if json_mode:
                    self._send_json(self._state_payload(data, active))
                else:
                    self._redirect('/?tab=' + quote(active))
                log_event(
                    'request',
                    method='POST',
                    path=parsed.path,
                    action=action,
                    tab=active,
                    ok=ok,
                    ip=self._ip(),
                    ms=round((time.monotonic() - started) * 1000, 1),
                )
                return

            if action == 'delete':
                tab_id = fields.get('tab', [''])[0]
                next_tab_id, data = delete_tab(tab_id)
                if json_mode:
                    self._send_json(self._state_payload(data, next_tab_id))
                else:
                    self._redirect('/?tab=' + quote(next_tab_id))
                log_event(
                    'request',
                    method='POST',
                    path=parsed.path,
                    action=action,
                    tab=tab_id,
                    next_tab=next_tab_id,
                    ip=self._ip(),
                    ms=round((time.monotonic() - started) * 1000, 1),
                )
                return

            self._not_found()
            log_event(
                'request',
                method='POST',
                path=parsed.path,
                action=action,
                status=404,
                ip=self._ip(),
                ms=round((time.monotonic() - started) * 1000, 1),
            )
            return

        if parsed.path != '/':
            self._not_found()
            log_event(
                'request',
                method='POST',
                path=parsed.path,
                status=404,
                ip=self._ip(),
                ms=round((time.monotonic() - started) * 1000, 1),
            )
            return

        data, tab_id = select_tab(requested_tab_id(parsed))
        if not find_tab(data, tab_id):
            self._not_found()
            log_event(
                'request',
                method='POST',
                path=parsed.path,
                status=404,
                tab=tab_id,
                ip=self._ip(),
                ms=round((time.monotonic() - started) * 1000, 1),
            )
            return
        text = fields.get('text', [''])[0]
        ok, revision = write_pad(tab_id, text)
        if not ok:
            self._not_found()
            log_event(
                'request',
                method='POST',
                path=parsed.path,
                status=404,
                tab=tab_id,
                ip=self._ip(),
                ms=round((time.monotonic() - started) * 1000, 1),
            )
            return
        broadcast(tab_id, text, revision)
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.send_header('X-Textpad-Revision', str(revision))
        self.end_headers()
        log_event(
            'request',
            method='POST',
            path=parsed.path,
            tab=tab_id,
            text_bytes=len(text.encode('utf-8')),
            ip=self._ip(),
            ms=round((time.monotonic() - started) * 1000, 1),
        )

    def log_message(self, fmt, *args):
        pass


class PadServer(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


if __name__ == '__main__':
    mirror_all_tabs(load_tabs())
    server = PadServer((HOST, PORT), PadHandler)
    print(f'localpad running on http://{HOST}:{PORT}')
    server.serve_forever()
