import type { Persona } from "../api";
import { ClockIcon, ShieldIcon } from "./Icons";

type ContextBadgeProps = {
  persona: Persona;
  activeContextId?: string;
};

export default function ContextBadge({ persona, activeContextId }: ContextBadgeProps) {
  const currentCtx =
    persona.contexts.find((c) => c.id === activeContextId) ??
    persona.contexts[0];

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border border-[#dce4dd] bg-[#f7faf7] px-4 py-3 text-xs text-[#536057]">
      <div className="flex items-center gap-2">
        <ShieldIcon className="h-4 w-4 text-[#28734d]" />
        <span className="font-medium">Đang trả lời theo ngữ cảnh:</span>
        <strong className="font-bold text-[#29332c]">
          {currentCtx ? currentCtx.label : persona.title || persona.name}
        </strong>
      </div>
      <span className="hidden h-4 w-px bg-[#d8dfd9] sm:block" />
      <div className="flex items-center gap-1.5">
        <ClockIcon className="h-3.5 w-3.5" />
        <span>Bản hiệu lực hôm nay · Độ mật: {persona.clearance}</span>
      </div>
    </div>
  );
}
