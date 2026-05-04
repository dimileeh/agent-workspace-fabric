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
        className={`fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-slate-200 bg-slate-50 shadow-2xl transition-transform duration-300 sm:w-[600px] xl:w-[800px] 2xl:w-[900px] ${
          isOpen ? "translate-x-0" : "translate-x-full"
        }`}
        inert={!isOpen ? true : undefined}
      >
        <div className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-slate-200 bg-white px-5 sm:px-6">
          <h2 className="min-w-0 truncate text-sm font-semibold text-slate-900">
            {title || "Workspace Inspector"}
          </h2>
          <button
            onClick={onClose}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 hover:text-slate-900"
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
