
import { AnimationEvent, HTMLAttributes, ReactNode, useEffect, useLayoutEffect, useState } from "react";
import { createPortal } from "react-dom";

let visibleModalLayers = 0;

export function ModalLayer({ open, className = "", children, onAnimationEnd, ...props }: {
  open: boolean;
  children: ReactNode;
} & HTMLAttributes<HTMLDivElement>) {
  const [snapshot, setSnapshot] = useState({ open, present: open, children });
  if (open !== snapshot.open) {
    setSnapshot({ open, present: true, children: open ? children : snapshot.children });
  } else if (open && children !== snapshot.children) {
    setSnapshot({ open, present: true, children });
  }

  useEffect(() => {
    if (open || !snapshot.present) return;
    const timer = window.setTimeout(() => setSnapshot(current => current.open ? current : { ...current, present: false }), 260);
    return () => window.clearTimeout(timer);
  }, [open, snapshot.present]);

  useLayoutEffect(() => {
    if (!open) return;
    // Blur the actual rendered application, independent of Chromium backdrop roots.
    // Dialogs live in a body portal and therefore stay outside this filtered tree.
    visibleModalLayers += 1;
    document.documentElement.dataset.modalBackground = "blurred";
    return () => {
      visibleModalLayers -= 1;
      if (visibleModalLayers === 0) delete document.documentElement.dataset.modalBackground;
    };
  }, [open]);

  if (!snapshot.present) return null;
  function finishExit(event: AnimationEvent<HTMLDivElement>) {
    onAnimationEnd?.(event);
    if (event.target === event.currentTarget && !open) {
      setSnapshot((current) => ({ ...current, present: false }));
    }
  }
  return createPortal(<div {...props} className={`${className} modal-layer`.trim()} data-modal-state={open ? "open" : "closing"} onAnimationEnd={finishExit}>
    {snapshot.children}
  </div>, document.body);
}
