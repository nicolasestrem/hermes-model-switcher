// react-dom/client shim over the host's global ReactDOM (React 18 exposes createRoot).
export function createRoot(...args) {
  return globalThis.ReactDOM.createRoot(...args);
}
