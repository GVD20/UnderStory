import { useCallback, useLayoutEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';

/** Keep a closing panel in its original grid until its own exit is complete. */
export function usePanelVisibility(initial: boolean, direction: 'left' | 'right') {
  const [visible, setVisible] = useState(initial);
  const [expanded, setExpanded] = useState(initial);
  const nodeRef = useRef<HTMLElement>(null);
  const intent = useRef(initial);
  const animation = useRef<Animation | null>(null);
  const first = useRef(true);
  useLayoutEffect(() => {
    if (first.current) { first.current = false; return; }
    const node = nodeRef.current;
    if (!visible || !node || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    animation.current = node.animate([{ opacity: 0, transform: `translateX(${direction === 'left' ? -16 : 16}px)` }, { opacity: 1, transform: 'none' }], { duration: 260, easing: 'cubic-bezier(.16,1,.3,1)' });
  }, [visible, direction]);
  useLayoutEffect(() => () => animation.current?.cancel(), []);
  const setAnimated = useCallback<Dispatch<SetStateAction<boolean>>>(next => {
    const target = typeof next === 'function' ? next(intent.current) : next;
    if (target === intent.current) return;
    intent.current = target;
    setExpanded(target);
    animation.current?.cancel();
    const node = nodeRef.current;
    if (target || !node || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      if (node) node.inert = false;
      setVisible(target);
      return;
    }
    node.inert = true;
    const exit = node.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: `translateX(${direction === 'left' ? -12 : 12}px)` }], { duration: 160, easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards' });
    animation.current = exit;
    void exit.finished.then(() => { if (!intent.current) setVisible(false); }).catch(() => {});
  }, [direction]);
  return [visible, setAnimated, nodeRef, expanded] as const;
}
