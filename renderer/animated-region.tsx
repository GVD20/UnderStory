import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';

/** Local, sequential exit/enter. The old scene stays in its own layout until it exits.
 * Content updates with the same key never animate. Loading may defer a scene swap. */
export function AnimatedRegion({ transitionKey, ready = true, className = '', children, onCommit, axis = 'horizontal', reverse = false }: {
  transitionKey: string; ready?: boolean; className?: string; children: ReactNode;
  onCommit?: () => void; axis?: 'horizontal' | 'vertical'; reverse?: boolean;
}) {
  const nodeRef = useRef<HTMLDivElement>(null);
  const latest = useRef({ transitionKey, ready, children, onCommit, className });
  latest.current = { transitionKey, ready, children, onCommit, className };
  const [scene, setScene] = useState({ key: transitionKey, children, className });
  const exiting = useRef(false);
  const entering = useRef(false);
  const animation = useRef<Animation | null>(null);
  const disposed = useRef(false);
  useLayoutEffect(() => {
    disposed.current = false;
    return () => { disposed.current = true; animation.current?.cancel(); };
  }, []);
  useLayoutEffect(() => {
    const node = nodeRef.current;
    if (!node || exiting.current) return;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (scene.key === transitionKey) {
      if (scene.children !== children || scene.className !== className) setScene(current => ({ ...current, children, className }));
      if (entering.current) {
        entering.current = false;
        animation.current?.cancel();
        latest.current.onCommit?.();
        if (!reduce) {
          animation.current = node.animate([
            { opacity: 0, transform: axis === 'horizontal' ? `translateX(${reverse ? -22 : 22}px)` : `translateY(${reverse ? -28 : 28}px)` },
            { opacity: 1, transform: 'none' },
          ], { duration: 280, easing: 'cubic-bezier(.16,1,.3,1)' });
        }
      }
      return;
    }
    if (!ready) return;
    animation.current?.cancel();
    const commit = () => {
      exiting.current = false;
      if (disposed.current) return;
      node.inert = false;
      if (!latest.current.ready) { animation.current?.cancel(); return; }
      entering.current = true;
      setScene({ key: latest.current.transitionKey, children: latest.current.children, className: latest.current.className });
    };
    if (reduce) { commit(); return; }
    exiting.current = true;
    node.inert = true;
    const exit = node.animate([
      { opacity: 1, transform: 'none' },
      { opacity: 0, transform: axis === 'horizontal' ? `translateX(${reverse ? 14 : -14}px)` : `translateY(${reverse ? 18 : -18}px)` },
    ], { duration: 140, easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards' });
    animation.current = exit;
    void exit.finished.then(commit).catch(() => { /* cancellation only during replacement or unmount */ });
  }, [transitionKey, ready, children, scene.key, scene.children, axis, className, reverse]);
  return <div ref={nodeRef} className={`motion-region ${scene.className}`} aria-busy={!ready || undefined}>{scene.children}</div>;
}
