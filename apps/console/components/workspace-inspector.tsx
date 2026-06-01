"use client";

import { X } from "lucide-react";
import { useEffect } from "react";

export function WorkspaceInspector({
  isOpen,
  onClose,
  title,
  children,
}: {
  isOpen: boolean;
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
}) {
  useEffect(() => {
    // Keep onClose referentially stable in parents, e.g. with useCallback, to avoid listener churn.
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isOpen) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose]);

  return (
    <>
      {isOpen && (
        <div
          className="fixed inset-0 z-40 bg-slate-900/20 backdrop-blur-sm sm:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}
      <div
        className={`fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-line bg-surface-2 shadow-2xl transition-transform duration-300 xl:w-[calc(100vw-440px)] 2xl:w-[calc(100vw-500px)] ${
          isOpen ? "translate-x-0" : "translate-x-full"
        }`}
        inert={!isOpen ? true : undefined}
      >
        <div className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-5 sm:px-6">
          <h2 className="min-w-0 truncate text-sm font-semibold text-fg">
            {title || "Workspace Inspector"}
          </h2>
          <button
            onClick={onClose}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] text-fg-muted hover:bg-surface-2 hover:text-fg"
            aria-label="Close inspector"
          >
            <X size={18} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto overflow-x-hidden px-4 py-4 sm:px-5 xl:px-6">
          {children}
        </div>
      </div>
    </>
  );
}
