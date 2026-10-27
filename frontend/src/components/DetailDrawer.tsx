import { useEffect, useId, useRef, type ReactNode } from "react";
import { X } from "lucide-react";

export function DetailDrawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = dialogRef.current;
    const previous = document.activeElement as HTMLElement | null;
    dialog?.showModal();
    return () => { dialog?.close(); previous?.focus({ preventScroll: true }); };
  }, []);
  return <dialog className="detail-drawer" ref={dialogRef} aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => { if (event.target === event.currentTarget) {
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
    } }}>
    <header className="drawer-header"><strong id={titleId}>{title}</strong>
      <button type="button" className="icon-button" aria-label="关闭详情" onClick={onClose}><X size={20} /></button></header>
    <div className="drawer-body">{children}</div>
  </dialog>;
}
