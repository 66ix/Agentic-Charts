"use client";

import { useEffect, useState } from "react";

/** True while the media query matches; false during server render and the first client render. */
export function useMediaQuery(query: string): boolean {
  const [match, setMatch] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setMatch(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return match;
}

/** Phones and narrow windows get the tabbed mobile layout. */
export const useIsMobile = () => useMediaQuery("(max-width: 767px)");
