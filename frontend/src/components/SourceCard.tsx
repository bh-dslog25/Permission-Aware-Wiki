import { useState } from "react";
import type { Source } from "../api";
import { BookIcon, ChevronDownIcon, ExternalLinkIcon } from "./Icons";

type SourceCardProps = {
  index: number;
  source: Source;
};

export default function SourceCard({ index, source }: SourceCardProps) {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <article className="overflow-hidden rounded-xl border border-[#e0e4e0] bg-white transition hover:border-[#cbd3cc]">
      <button
        aria-expanded={isOpen}
        className="flex w-full items-center gap-3 p-3.5 text-left"
        onClick={() => setIsOpen((open) => !open)}
        type="button"
      >
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-[#edf4ee] text-[11px] font-bold text-[#286b47]">
          {index}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-bold text-[#303933]">{source.title}</span>
          <span className="mt-0.5 block truncate text-[11px] text-[#7a837c]">
            {source.section} · Hiệu lực {source.effectiveDate}
          </span>
        </span>
        <ChevronDownIcon
          className={`h-4 w-4 shrink-0 text-[#7e867f] transition ${isOpen ? "rotate-180" : ""}`}
        />
      </button>

      {isOpen && (
        <div className="border-t border-[#e8ebe8] bg-[#fafbf9] px-4 py-4">
          <div className="mb-3 flex items-center gap-2 text-[11px] font-semibold text-[#727b74]">
            <BookIcon className="h-3.5 w-3.5" />
            <span>{source.version}</span>
            <span>·</span>
            <span>{source.owner}</span>
          </div>
          <blockquote className="border-l-2 border-[#88b399] pl-3 text-xs leading-5 text-[#5d665f]">
            “{source.excerpt}”
          </blockquote>
          <button
            className="mt-4 flex items-center gap-1.5 text-xs font-bold text-[#216942] transition hover:text-[#124d2f]"
            type="button"
          >
            Mở tài liệu
            <ExternalLinkIcon className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
    </article>
  );
}
