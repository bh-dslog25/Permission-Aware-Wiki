export type ContextOption = {
  id: string;
  label: string;
};

export type User = {
  id: string;
  name: string;
  email: string;
  title: string;
  initials: string;
  role: string;
  clearance: string;
  description: string;
  contexts: ContextOption[];
  defaultContextId: string;
};

export type Persona = {
  id: string;
  name: string;
  email: string;
  title: string;
  role: string;
  clearance: string;
  description: string;
  contexts: ContextOption[];
  defaultContextId: string;
  initials: string;
};

export type Source = {
  id: string;
  title: string;
  section: string;
  excerpt: string;
  version: string;
  effectiveDate: string;
  owner: string;
};

export type AnswerStatus = "answered" | "denied" | "insufficient";

export type ChatAnswer = {
  id: string;
  status: AnswerStatus;
  title?: string;
  content: string;
  details?: string[];
  sources: Source[];
  answeredAt: string;
};

type BackendCitation = {
  index: number;
  document_id: string;
  chunk_id: string;
  title: string;
  excerpt: string;
  quote_verified: boolean;
  version_number?: string | null;
};

type BackendAnswer = {
  status: "answered" | "abstained" | "needs_clarification";
  answer: string;
  applies_to?: string | null;
  context_label?: string | null;
  effective_at?: string | null;
  as_of?: string | null;
  citations: BackendCitation[];
  refused: boolean;
  refusal_reason?: string | null;
  request_id: string;
};

type BackendPersona = {
  id: string;
  name: string;
  email: string;
  title: string;
  role: string;
  clearance: string;
  description: string;
  contexts: Array<{ id: string; label: string }>;
  default_context_id?: string | null;
  is_persona?: boolean;
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

function getInitials(name: string): string {
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }
  return name.slice(0, 2).toUpperCase();
}

export function toPersona(backendP: BackendPersona): Persona {
  return {
    id: backendP.id,
    name: backendP.name,
    email: backendP.email,
    title: backendP.title,
    role: backendP.role,
    clearance: backendP.clearance,
    description: backendP.description,
    contexts: backendP.contexts || [],
    defaultContextId: backendP.default_context_id || (backendP.contexts?.[0]?.id ?? ""),
    initials: getInitials(backendP.name),
  };
}

export function toUser(backendP: BackendPersona): User {
  return {
    id: backendP.id,
    name: backendP.name,
    email: backendP.email,
    title: backendP.title,
    role: backendP.role,
    clearance: backendP.clearance,
    description: backendP.description,
    contexts: backendP.contexts || [],
    defaultContextId: backendP.default_context_id || (backendP.contexts?.[0]?.id ?? ""),
    initials: getInitials(backendP.name),
  };
}

export async function fetchPersonas(): Promise<Persona[]> {
  try {
    const res = await fetch(`${API_BASE_URL}/api/v1/personas`);
    if (!res.ok) {
      throw new Error("Không thể tải danh sách tài khoản từ máy chủ.");
    }
    const data = (await res.json()) as BackendPersona[];
    return data.map(toPersona);
  } catch (err) {
    console.warn("fetchPersonas error, using fallback accounts:", err);
    return [
      {
        id: "u_minh_anh",
        name: "Nguyễn Minh Anh",
        email: "minh.anh@appota.com",
        title: "Chuyên viên Kinh doanh",
        role: "employee",
        clearance: "internal",
        description: "Nhân viên Kinh doanh, Công ty A",
        contexts: [{ id: "ctx_minh_anh_main", label: "Nhóm Kinh doanh 1" }],
        defaultContextId: "ctx_minh_anh_main",
        initials: "MA",
      },
      {
        id: "u_quoc_bao",
        name: "Trần Quốc Bảo",
        email: "quoc.bao@appota.com",
        title: "Kỹ sư Backend",
        role: "employee",
        clearance: "internal",
        description: "Nhân viên Kỹ thuật, Công ty B",
        contexts: [{ id: "ctx_quoc_bao_main", label: "Nhóm Backend" }],
        defaultContextId: "ctx_quoc_bao_main",
        initials: "QB",
      },
      {
        id: "u_thu_ha",
        name: "Lê Thu Hà",
        email: "thu.ha@appota.com",
        title: "Trưởng phòng Kinh doanh – Công ty A",
        role: "manager",
        clearance: "confidential",
        description: "Quản lý kiêm nhiệm: Trưởng phòng Kinh doanh Công ty A và quản lý nhóm QA Công ty B",
        contexts: [
          { id: "ctx_thu_ha_a", label: "Nhóm Kinh doanh 2 (Công ty A)" },
          { id: "ctx_thu_ha_b", label: "Nhóm QA (Công ty B)" },
        ],
        defaultContextId: "ctx_thu_ha_a",
        initials: "TH",
      },
    ];
  }
}

export async function authenticate(identifier: string, password: string): Promise<User> {
  const cleanId = identifier.trim();
  const cleanPass = password.trim();

  if (!cleanId) {
    throw new Error("Vui lòng nhập email hoặc mã tài khoản.");
  }

  const response = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      username_or_email: cleanId,
      password: cleanPass,
    }),
  });

  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}));
    throw new Error(
      errorBody.detail || "Đăng nhập không thành công. Hãy kiểm tra tài khoản trong org.yaml.",
    );
  }

  const result = (await response.json()) as { status: string; user: BackendPersona };
  return toUser(result.user);
}

function formatTime() {
  return new Intl.DateTimeFormat("vi-VN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date());
}

function formatDate(value?: string | null) {
  if (!value) return "không rõ";
  return new Intl.DateTimeFormat("vi-VN").format(new Date(`${value}T00:00:00`));
}

function statusOf(answer: BackendAnswer): AnswerStatus {
  if (answer.status === "answered") return "answered";
  return answer.refusal_reason === "not_found" || answer.refusal_reason === "system_error"
    ? "denied"
    : "insufficient";
}

export async function askQuestion(
  question: string,
  userId: string,
  contextId: string,
): Promise<ChatAnswer> {
  const response = await fetch(`${API_BASE_URL}/api/v1/ask`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Persona-Id": userId,
    },
    body: JSON.stringify({
      question,
      context_id: contextId,
    }),
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? "Không gọi được backend hỏi đáp.");
  }

  const answer = (await response.json()) as BackendAnswer;
  const sources = answer.citations.map((citation) => ({
    id: citation.chunk_id,
    title: citation.title,
    section: citation.chunk_id,
    excerpt: citation.excerpt,
    version: citation.version_number ? `Phiên bản ${citation.version_number}` : "Phiên bản không rõ",
    effectiveDate: formatDate(answer.effective_at ?? answer.as_of),
    owner: answer.applies_to ?? answer.context_label ?? "Nguồn được cấp quyền",
  }));

  const details = [
    answer.applies_to ? `Phạm vi áp dụng: ${answer.applies_to}.` : null,
    answer.as_of ? `Tra cứu tại thời điểm: ${formatDate(answer.as_of)}.` : null,
    answer.request_id ? `Request ID: ${answer.request_id}.` : null,
  ].filter(Boolean) as string[];

  return {
    id: answer.request_id,
    status: statusOf(answer),
    title:
      answer.status === "answered"
        ? "Câu trả lời dựa trên nguồn được phép"
        : answer.status === "needs_clarification"
          ? "Cần làm rõ câu hỏi"
          : undefined,
    content: answer.answer,
    details,
    sources,
    answeredAt: formatTime(),
  };
}
