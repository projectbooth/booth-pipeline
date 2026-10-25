import { useState } from "react";

/** Whether a canvas is expanded, remembered per browser under `key`. A per-viewer convenience: if
 *  storage is unavailable (a private window, blocked site data) it simply starts compact. */
export function useExpandedCanvas(key: string): [boolean, () => void] {
  const [expanded, setExpanded] = useState(() => {
    try {
      return localStorage.getItem(key) === "1";
    } catch {
      return false;
    }
  });
  function toggle() {
    setExpanded((was) => {
      try {
        localStorage.setItem(key, was ? "0" : "1");
      } catch {
        // nothing to do: the canvas just won't remember it
      }
      return !was;
    });
  }
  return [expanded, toggle];
}
