// Host React shim: the Dashboard provides React globally; we never bundle a second React.
const React = globalThis.React;
export default React;
export const { useState, useEffect, useMemo, useRef, Fragment, createElement } = React;
