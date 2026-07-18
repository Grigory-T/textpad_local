const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('pad.py', 'utf8');
const match = source.match(/function editIndentation[\s\S]*?\n}\nfunction renderTabs/);
if (!match) throw new Error('editIndentation function not found');

const context = {};
const renderedSource = match[0]
  .replace(/\nfunction renderTabs$/, '')
  .replace(/\\\\/g, String.fromCharCode(92));
vm.runInNewContext(renderedSource, context);

function check(name, input, start, end, outdent, expected) {
  const actual = context.editIndentation(input, start, end, outdent);
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${name}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
  }
}

check('insert tab at cursor', 'one', 1, 1, false,
  {value: 'o\tne', start: 2, end: 2});
check('indent selected lines', 'one\ntwo\nthree', 1, 7, false,
  {value: '\tone\n\ttwo\nthree', start: 2, end: 9});
check('exclude line at selection end boundary', 'one\ntwo', 0, 4, false,
  {value: '\tone\ntwo', start: 0, end: 5});
const mixedIndent = '\tone\n    two\n  three';
check('outdent tabs and spaces', mixedIndent, 0, mixedIndent.length, true,
  {value: 'one\ntwo\nthree', start: 0, end: 13});
check('outdent current line at cursor', 'one\n    two', 9, 9, true,
  {value: 'one\ntwo', start: 5, end: 5});

console.log('indentation tests passed');
