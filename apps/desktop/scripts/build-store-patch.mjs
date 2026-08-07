import { readFileSync, writeFileSync } from 'node:fs'

const DST = '/tmp/pristine-store/package/dist/useAui-patched.js'

let s = readFileSync('/tmp/save-useaui', 'utf8')

// Fix 1: clientFunction guard - use value.state instead of typeof value.methods
const FIX1_OLD = "\t\t\tif (value && typeof value.methods === 'function') {\n\t\t\t\treturn value.methods;\n\t\t\t}"
const FIX1_NEW = "\t\t\tif (value && value.state != null) {\n\t\t\t\treturn value.methods;\n\t\t\t}"
if (s.includes(FIX1_OLD)) { s = s.replace(FIX1_OLD, FIX1_NEW); console.log('fix 1 applied') }
else { console.log('fix 1 OLD not found') }

// Add isFocused/disabled to ALL composerState shapes
const enriched1 = `const composerState = {
\t\t\t\t\t\t\ttext: "",
\t\t\t\t\t\t\tcanSend: false,
\t\t\t\t\t\t\tisLoading: false,
\t\t\t\t\t\t\tattachments: [],
\t\t\t\t\t\t\tisFocused: false,
\t\t\t\t\t\t\tdisabled: false,
\t\t\t\t\t\t};`
const match1 = `const composerState = { text: "", canSend: false, isLoading: false, attachments: [] };`
s = s.split(match1).join(enriched1)

const enriched2 = `const composerState = {
\t\t\t\t\t\t\ttext: '',
\t\t\t\t\t\t\tcanSend: false,
\t\t\t\t\t\t\tisLoading: false,
\t\t\t\t\t\t\tattachments: [],
\t\t\t\t\t\t\tisFocused: false,
\t\t\t\t\t\t\tdisabled: false,
\t\t\t\t\t\t};`
const match2 = `const composerState = { text: '', canSend: false, isLoading: false, attachments: [] };`
s = s.split(match2).join(enriched2)

// Stub object fallback at line 103
const stub_match = `?? { text: "", canSend: false, isLoading: false, attachments: [], setText: () => {}, appendText: () => {}, submit: () => {}, cancel: () => {}, subscribe: () => () => {}, on: () => () => {} }`
const stub_new = `?? { text: "", canSend: false, isLoading: false, attachments: [], isFocused: false, disabled: false, setText: () => {}, appendText: () => {}, submit: () => {}, cancel: () => {}, subscribe: () => () => {}, on: () => () => {} }`
if (s.includes(stub_match)) { s = s.replace(stub_match, stub_new); console.log('stub fix applied') }

// INNER composer proxy: use regex to find the block and replace regardless of indent
const innerGetPattern = /(get\(t2, p2\) \{\s*\n\s*if \(p2 === "getState"\) return \(\) => composerState;\s*\n\s*if \(p2 === "subscribe" \|\| p2 === "on"\) return \(\) => \(\) => \{\};\s*\n\s*if \(p2 === "setText" \|\| p2 === "appendText" \|\| p2 === "submit"\) return \(\) => \{\};\s*\n\s*return undefined;\s*\n\s*\},)/
const newInnerGet = `get(t2, p2) {
\t\t\t\t\t\t\t\t\tif (p2 === "getState") return () => composerState;
\t\t\t\t\t\t\t\t\tif (p2 === "subscribe" || p2 === "on") return () => () => {};
\t\t\t\t\t\t\t\t\tif (p2 in composerState) return composerState[p2];
\t\t\t\t\t\t\t\t\tif (p2 === "setText" || p2 === "appendText" || p2 === "submit" || p2 === "cancel" || p2 === "reset" || p2 === "focus" || p2 === "blur" || p2 === "addAttachment" || p2 === "setAttachments" || p2 === "removeAttachment") return () => {};
\t\t\t\t\t\t\t\t\treturn undefined;
\t\t\t\t\t\t\t\t},`

if (innerGetPattern.test(s)) {
  s = s.replace(innerGetPattern, newInnerGet)
  console.log('inner get handler fixed')
} else {
  console.log('inner get pattern NOT matched')
}

const innerHasPattern = /(has\(t2, p2\) \{\s*\n\s*return p2 === "getState" \|\| p2 === "subscribe" \|\| p2 === "on" \|\| p2 === "setText" \|\| p2 === "appendText" \|\| p2 === "submit";\s*\n\s*\},)/
const newInnerHas = `has(t2, p2) {
\t\t\t\t\t\t\t\t\treturn p2 === "getState" || p2 === "subscribe" || p2 === "on" || p2 === "setText" || p2 === "appendText" || p2 === "submit" || p2 === "cancel" || p2 === "reset" || p2 === "focus" || p2 === "blur" || p2 === "addAttachment" || p2 === "setAttachments" || p2 === "removeAttachment" || p2 in composerState;
\t\t\t\t\t\t\t\t},`
if (innerHasPattern.test(s)) {
  s = s.replace(innerHasPattern, newInnerHas)
  console.log('inner has handler fixed')
}

// fn composer field assignments — use regex
const fnPattern = /(fn\.text = '';\s*\n\s*fn\.canSend = false;\s*\n\s*fn\.isLoading = false;\s*\n\s*fn\.attachments = \[\];)/
const fnReplace = `fn.text = '';
\t\t\t\t\t\t\tfn.canSend = false;
\t\t\t\t\t\t\tfn.isLoading = false;
\t\t\t\t\t\t\tfn.attachments = [];
\t\t\t\t\t\t\tfn.isFocused = false;
\t\t\t\t\t\t\tfn.disabled = false;`
const fnMatches = s.match(new RegExp(fnPattern.source, 'g'))
console.log('fn composer fields match count:', fnMatches ? fnMatches.length : 0)
if (fnMatches) {
  s = s.replace(new RegExp(fnPattern.source, 'g'), fnReplace)
}

// Build a shape-stable composer stub returned when HermesRuntime has not
// yet exposed a real composer. With the post-build-patch Step 1D changed
// to passthrough (getClientState now returns its input), the composer
// stub returned by `clientFunction.composer()` IS the composer state
// shape — every `useAuiState(s => s.composer.X)` selector resolves
// through getProxiedAssistantState → clientFunction.composer() → the
// stub itself, and `.X` reads straight off the stub.
//
// Verified 2026-08-06 — without this, the workspace pane boot fires
// `TypeError: Cannot read properties of undefined (reading 'canSend')`
// because HermesRuntime doesn't expose a composer field, and the
// assistant-ui library's ComposerPrimitive.If selector reads
// `state.composer.isEditing` etc. on every render.
const composerStubOld = `clientFunction.composer = () => clientFunction.threads()?.thread('main')?.composer?.() ?? { text: "", canSend: false, isLoading: false, attachments: [], isFocused: false, disabled: false, setText: () => {}, appendText: () => {}, submit: () => {}, cancel: () => {}, subscribe: () => () => {}, on: () => () => {} };`

const composerStubNew = `clientFunction.composer = () => {
\t\tconst _composerState = {
\t\t\ttext: '',
\t\t\tcanSend: false,
\t\t\tisLoading: false,
\t\t\tattachments: [],
\t\t\tisFocused: false,
\t\t\tdisabled: false,
\t\t\tisEditing: false,
\t\t\tdictation: null,
\t\t\tisEmpty: true,
\t\t\tvalue: '',
\t\t\trole: 'user',
\t\t\tquote: undefined,
\t\t\tcapabilities: { cancel: false, queue: false, attachmentAccept: '*/*' },
\t\t\tcapabilitiesConfig: {},
\t\t\tisLastQueued: false,
\t\t\tsetText: () => {},
\t\t\tappendText: () => {},
\t\t\tsubmit: () => {},
\t\t\tcancel: () => {},
\t\t\treset: () => {},
\t\t\tfocus: () => {},
\t\t\tblur: () => {},
\t\t\taddAttachment: () => {},
\t\t\tsetAttachments: () => {},
\t\t\tremoveAttachment: () => {},
\t\t\tsubscribe: () => () => {},
\t\t\ton: () => () => {},
\t\t};
\t\tconst _realComposer = clientFunction.threads()?.thread('main')?.composer?.();
\t\tif (_realComposer) return _realComposer;
\t\treturn _composerState;
\t};`

if (s.includes(composerStubOld)) {
  s = s.replace(composerStubOld, composerStubNew)
  console.log('composer stub updated to direct state-shape return (Step 1D passthrough)')
} else {
  console.log('composerStubOld not found')
}

// Remove unused getClientStateOrEmpty import
s = s.replace(
  'import { useClientResource, getClientStateOrEmpty } from "./useClientResource.js";',
  'import { useClientResource } from "./useClientResource.js";'
)

if (!s.startsWith('// PATCH_ACTIVE')) {
  s = '// PATCH_ACTIVE_2026_AUG_02_HERMES_DESKTOP\n' + s
}

writeFileSync(DST, s)
console.log('Wrote bytes:', s.length)