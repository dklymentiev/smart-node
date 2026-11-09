import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'public/next2/js/app.js'), 'utf8');
function extract(name) {
  const found = src.match(new RegExp(`^function ${name}\\([\\s\\S]*?^}`, 'm'));
  assert.ok(found, `${name} missing`);
  return found[0];
}
function extractConst(name) {
  const found = src.match(new RegExp(`^const ${name} = [\\s\\S]*?^};`, 'm'));
  assert.ok(found, `${name} missing`);
  return found[0];
}

class Element {
  constructor(className = '') {
    this.className = className;
    this.classList = {
      contains: (name) => this.className.split(' ').includes(name),
      add: (name) => { if (!this.classList.contains(name)) this.className += ` ${name}`; },
      remove: (name) => { this.className = this.className.split(' ').filter((part) => part !== name).join(' '); },
      toggle: (name, force) => {
        const add = force ?? !this.classList.contains(name);
        if (add) this.classList.add(name); else this.classList.remove(name);
        return add;
      },
    };
    this.children = [];
    this.isConnected = true;
    this.textContent = '';
    this.innerHTML = '';
    this.scrollHeight = 100;
  }
  querySelector(selector) {
    return this.children.find((child) => child.className.split(' ').includes(selector.slice(1))) || null;
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(child) { this.children.push(child); child.parent = this; }
  insertBefore(child, before) {
    this.children.splice(this.children.indexOf(before), 0, child);
    child.parent = this;
  }
  insertAdjacentElement(_position, child) { this.parent.insertBefore(child, this.parent.children[this.parent.children.indexOf(this) + 1]); }
  addEventListener(event, handler) { this[`on${event}`] = handler; }
  setAttribute(name, value) { this[name] = value; }
  click() { this.onclick?.({ stopPropagation() {} }); }
  remove() {
    if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
  }
}

const render = new Function('document', 'Element', `
  const STEPS_KEPT = 40;
  // tool steps are shown to an admin only; this test is about how they read
  const PANEL_ROLES = ['super-admin', 'admin'];
  const meIdentity = { role: 'admin' };
  const esc = (s) => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;');
  ${extract('renderLiveText')}
  ${extract('consoleOpen')}
  // the words come from core-api's tool registry (GET /api/tools/labels); two rows of it
  let _toolLabels = { Read: { kind: 'builtin', doing: 'Reading a file' }, Bash: { kind: 'builtin', doing: 'Running a command' } };
  ${extract('toolDoing')}
  ${extract('plainStep')}
  ${extract('renderRecent')}
  ${extract('renderSteps')}
  return { renderLiveText, renderSteps };
`)({ createElement: () => new Element() }, Element);

test('live answer keeps full text in its own node and tools stay in the scroll box', () => {
  const root = new Element();
  root.appendChild(new Element('sn-think'));
  const long = 'Visible answer ' + 'x'.repeat(500);
  const turn = { el: root, liveText: long, steps: [
    { kind: 'say', text: long }, { kind: 'tool', text: 'Read: notes.txt' },
  ] };
  render.renderLiveText(turn);
  render.renderSteps(turn);
  assert.equal(root.querySelector('.sn-live-text').textContent, long);
  assert.match(root.querySelector('.sn-steps').innerHTML, /Read: notes.txt/);
  assert.doesNotMatch(root.querySelector('.sn-steps').innerHTML, /Visible answer/);
  turn.steps.push({ kind: 'tool', text: 'Read: notes.txt' });
  render.renderSteps(turn);
  assert.match(root.querySelector('.sn-steps').innerHTML, /×2/);
  assert.equal((root.querySelector('.sn-steps').innerHTML.match(/Read: notes.txt/g) || []).length, 1);
  turn.liveText += ' done';
  render.renderLiveText(turn);
  assert.equal(root.children.filter((c) => c.className === 'sn-live-text').length, 1);
  assert.equal(root.querySelector('.sn-live-text').textContent, long + ' done');
});

test("the last tools show in the registry's words, the command never, and the model's description wins", () => {
  const root = new Element();
  root.appendChild(new Element('sn-think'));
  const turn = { el: root, steps: [
    { kind: 'tool', text: 'Read: /srv/agents/x/notes.txt' },
    { kind: 'tool', text: 'Bash: GITLAB_TOKEN=$(curl -s http://x)', desc: 'Fetch the token' },
    { kind: 'tool', text: 'Bash: pytest -q' },
    { kind: 'tool', text: 'mcp__planner__planner_comment: {}' },
  ] };
  render.renderSteps(turn);
  const html = root.querySelector('.sn-recent').innerHTML;
  assert.doesNotMatch(html, /Reading a file/, 'only the last three show');
  assert.match(html, /Fetch the token/);
  assert.match(html, /Running a command/);
  assert.match(html, /Planner comment/, 'a tool the registry does not word reads as its name');
  assert.doesNotMatch(html, /GITLAB_TOKEN|pytest|notes\.txt/);
  turn.steps.push({ kind: 'tool', text: 'mcp__planner__planner_comment: {}' });
  render.renderSteps(turn);
  assert.match(root.querySelector('.sn-recent').innerHTML, /×2/);
  const solo = new Element();
  solo.appendChild(new Element('sn-think'));
  render.renderSteps({ el: solo, steps: [{ kind: 'tool', text: 'Read: /srv/agents/x/notes.txt' }] });
  assert.match(solo.querySelector('.sn-recent').innerHTML, /Reading a file/);
  assert.doesNotMatch(solo.querySelector('.sn-recent').innerHTML, /notes\.txt/);
});
