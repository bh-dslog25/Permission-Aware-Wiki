import React, { useEffect, useState } from "react";
import {
  fetchPendingReviews,
  submitDocumentReview,
  type PendingReviewItem,
  type User,
} from "../api";
import {
  ArrowRightIcon,
  CheckIcon,
  ShieldIcon,
  SparklesIcon,
} from "./Icons";

type Props = {
  user: User;
  onBackToChat: () => void;
  onLogout: () => void;
};

type FilterTab = "pending" | "reviewed" | "all";

export default function OwnerReviewPortal({ user, onBackToChat, onLogout }: Props) {
  const [currentTab, setCurrentTab] = useState<FilterTab>("pending");
  const [items, setItems] = useState<PendingReviewItem[]>([]);
  const [stats, setStats] = useState<{ pending: number; reviewed: number; total: number } | null>(null);
  const [selectedDocId, setSelectedDocId] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string>("");
  const [successMsg, setSuccessMsg] = useState<string>("");

  // Form review state
  const [decision, setDecision] = useState<"confirmed" | "conflicting" | "unverified">("confirmed");
  const [justification, setJustification] = useState<string>("");
  const [clause, setClause] = useState<string>("");

  const loadData = async (tabToLoad: FilterTab = currentTab) => {
    setLoading(true);
    setError("");
    try {
      const res = await fetchPendingReviews(user.id, tabToLoad);
      if (!Array.isArray(res.items)) {
        throw new Error("API không trả về danh sách tài liệu hợp lệ.");
      }
      setItems(res.items);
      setStats(res.stats ?? null);
      if (res.items.length > 0) {
        // Giữ doc đang chọn nếu nó vẫn tồn tại trong list, nếu không chọn item đầu tiên
        const exists = res.items.some((d) => d.document_id === selectedDocId);
        if (!exists) {
          setSelectedDocId(res.items[0].document_id);
        }
      } else {
        setSelectedDocId("");
      }
    } catch (err: any) {
      setError(err.message || "Không thể tải danh sách tài liệu.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData(currentTab);
  }, [user.id, currentTab]);

  const selectedDoc = items.find((it) => it.document_id === selectedDocId);

  // Khi chọn một document mới, sync lại giá trị form từ last_review hoặc trạng thái hiện tại
  useEffect(() => {
    if (selectedDoc) {
      if (selectedDoc.last_review) {
        setDecision(selectedDoc.last_review.decision);
        setJustification(selectedDoc.last_review.justification || "");
        setClause(selectedDoc.last_review.clause || "");
      } else {
        setDecision(selectedDoc.parent_conformance);
        setJustification("");
        setClause("");
      }
    }
  }, [selectedDocId]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedDoc) return;
    if (!justification.trim()) {
      setError("Vui lòng nhập lý do phê duyệt hoặc ghi nhận căn cứ.");
      return;
    }

    setSubmitting(true);
    setError("");
    setSuccessMsg("");

    try {
      const res = await submitDocumentReview(selectedDoc.document_id, user.id, {
        decision,
        justification,
        clause,
      });
      setSuccessMsg(res.message);
      // Refresh list
      await loadData(currentTab);
    } catch (err: any) {
      setError(err.message || "Lỗi khi lưu kết luận phê duyệt.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex h-screen w-screen flex-col bg-[#f7f8f6] text-[#1c211d]">
      {/* Top Header */}
      <header className="flex h-16 shrink-0 items-center justify-between border-b border-[#e2e6e2] bg-white px-6">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[#165c3a] text-white">
            <ShieldIcon className="h-5 w-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-sm font-bold text-[#1c211d]">Knowledge Owner Portal</h1>
              <span className="rounded-full bg-[#e8f3ed] px-2 py-0.5 text-[10px] font-semibold text-[#165c3a]">
                Quản trị & Phê duyệt tri thức
              </span>
            </div>
            <p className="text-xs text-[#788078]">
              Rà soát tính hợp chuẩn, đối chiếu chính sách khung và giám sát văn bản đã duyệt
            </p>
          </div>
        </div>

        <div className="flex items-center gap-4">
          <div className="text-right">
            <p className="text-xs font-semibold text-[#1c211d]">{user.name}</p>
            <p className="text-[11px] text-[#788078]">
              {user.title || "Knowledge Owner"} · Clearance:{" "}
              <span className="uppercase text-[#165c3a]">{user.clearance}</span>
            </p>
          </div>

          <button
            onClick={onBackToChat}
            className="flex items-center gap-1.5 rounded-lg border border-[#d8ded8] bg-white px-3 py-1.5 text-xs font-semibold text-[#3c453e] hover:bg-[#f0f4f0]"
          >
            <SparklesIcon className="h-3.5 w-3.5 text-[#165c3a]" />
            Hỏi đáp Wiki
          </button>

          <button
            onClick={onLogout}
            className="rounded-lg border border-transparent px-2.5 py-1.5 text-xs font-medium text-[#788078] hover:bg-[#f1f3f1] hover:text-[#1c211d]"
          >
            Đăng xuất
          </button>
        </div>
      </header>

      {/* Main Content Area */}
      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar: Danh sách văn bản với bộ lọc Tabs */}
        <aside className="w-84 shrink-0 border-r border-[#e2e6e2] bg-white p-4 flex flex-col">
          {/* Filter Tabs */}
          <div className="mb-3 flex rounded-xl bg-[#f0f3ef] p-1 text-xs font-semibold">
            <button
              onClick={() => setCurrentTab("pending")}
              className={`flex-1 rounded-lg py-1.5 text-center transition ${
                currentTab === "pending"
                  ? "bg-white text-[#165c3a] shadow-sm font-bold"
                  : "text-[#657067] hover:text-[#1c211d]"
              }`}
            >
              Cần duyệt ({stats?.pending ?? "—"})
            </button>
            <button
              onClick={() => setCurrentTab("reviewed")}
              className={`flex-1 rounded-lg py-1.5 text-center transition ${
                currentTab === "reviewed"
                  ? "bg-white text-[#165c3a] shadow-sm font-bold"
                  : "text-[#657067] hover:text-[#1c211d]"
              }`}
            >
              Đã duyệt ({stats?.reviewed ?? "—"})
            </button>
            <button
              onClick={() => setCurrentTab("all")}
              className={`flex-1 rounded-lg py-1.5 text-center transition ${
                currentTab === "all"
                  ? "bg-white text-[#165c3a] shadow-sm font-bold"
                  : "text-[#657067] hover:text-[#1c211d]"
              }`}
            >
              Tất cả ({stats?.total ?? "—"})
            </button>
          </div>

          <div className="mb-2 flex items-center justify-between px-1">
            <span className="text-[11px] font-bold uppercase tracking-wider text-[#788078]">
              {currentTab === "pending"
                ? "Văn bản chờ xử lý"
                : currentTab === "reviewed"
                ? "Chính sách đã duyệt"
                : "Toàn bộ tài liệu"}
            </span>
            <button
              onClick={() => loadData(currentTab)}
              className="text-xs text-[#165c3a] hover:underline"
              title="Tải lại danh sách"
            >
              Làm mới
            </button>
          </div>

          {error && (
            <p
              className="mb-3 rounded-lg border border-[#fecaca] bg-[#fef2f2] p-3 text-xs text-[#991b1b]"
              role="alert"
            >
              {error}
            </p>
          )}

          {!loading && !error && !stats && (
            <p
              className="mb-3 rounded-lg border border-[#fde68a] bg-[#fffbeb] p-3 text-xs text-[#92400e]"
              role="status"
            >
              Backend đang chạy phiên bản cũ, thiếu thống kê duyệt. Hãy khởi động lại backend để cập nhật đầy đủ.
            </p>
          )}

          {loading ? (
            <p className="py-8 text-center text-xs text-[#788078]">Đang tải dữ liệu...</p>
          ) : items.length === 0 ? (
            <div className="my-auto rounded-xl border border-dashed border-[#d8ded8] p-6 text-center">
              <CheckIcon className="mx-auto h-6 w-6 text-[#165c3a]" />
              <p className="mt-2 text-xs font-semibold text-[#1c211d]">
                {currentTab === "pending" ? "Không có văn bản tồn đọng" : "Không có văn bản trong mục này"}
              </p>
              <p className="mt-1 text-[11px] text-[#788078]">
                {currentTab === "pending"
                  ? "Tất cả chính sách đã được xác thực hoàn tất."
                  : "Chuyển sang tab khác để xem tài liệu."}
              </p>
            </div>
          ) : (
            <div className="flex-1 space-y-2 overflow-y-auto pr-1">
              {items.map((doc) => {
                const isSelected = doc.document_id === selectedDocId;
                const isConfirmed = doc.parent_conformance === "confirmed";
                const isConflicting = doc.parent_conformance === "conflicting";

                return (
                  <button
                    key={doc.document_id}
                    onClick={() => {
                      setSelectedDocId(doc.document_id);
                      setSuccessMsg("");
                      setError("");
                    }}
                    className={`w-full text-left rounded-xl p-3 transition border ${
                      isSelected
                        ? "border-[#165c3a] bg-[#f0f8f3] shadow-sm"
                        : "border-[#e6eae6] hover:bg-[#fafbfa]"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <p className="text-xs font-bold leading-tight text-[#1c211d]">
                        {doc.title}
                      </p>
                      <span
                        className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${
                          isConfirmed
                            ? "bg-[#ecfdf5] text-[#065f46]"
                            : isConflicting
                            ? "bg-[#fee2e2] text-[#991b1b]"
                            : "bg-[#fef3c7] text-[#92400e]"
                        }`}
                      >
                        {doc.parent_conformance}
                      </span>
                    </div>
                    <div className="mt-2 flex items-center justify-between text-[11px] text-[#788078]">
                      <span>Đơn vị: {doc.owner_unit_name}</span>
                      <span>v{doc.version}</span>
                    </div>

                    {/* Hiển thị tóm tắt người duyệt nếu đã có */}
                    {doc.last_review && (
                      <div className="mt-1.5 border-t border-[#e8ece8] pt-1 text-[10px] text-[#4b5563] flex items-center justify-between">
                        <span className="truncate">Duyệt bởi: <b>{doc.last_review.user_id}</b></span>
                        <span className="text-[#065f46] font-semibold">✓ Đã lưu Audit</span>
                      </div>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </aside>

        {/* Center & Right: Dual View So sánh & Form Phê duyệt */}
        {selectedDoc ? (
          <main className="flex flex-1 overflow-hidden">
            {/* Dual comparison view */}
            <div className="flex-1 overflow-y-auto p-6 space-y-6">
              {/* Thông báo kết quả */}
              {successMsg && (
                <div className="rounded-xl bg-[#ecfdf5] border border-[#a7f3d0] p-4 text-xs font-semibold text-[#065f46]">
                  {successMsg}
                </div>
              )}
              {error && (
                <div className="rounded-xl bg-[#fef2f2] border border-[#fecaca] p-4 text-xs font-semibold text-[#991b1b]">
                  {error}
                </div>
              )}

              {/* Banner hiển thị trạng thái hiện tại của tài liệu đã duyệt */}
              {selectedDoc.last_review && (
                <div className="rounded-xl bg-[#f0f9f4] border border-[#bbf7d0] p-4 text-xs text-[#166534]">
                  <div className="flex items-center justify-between mb-1">
                    <span className="font-bold flex items-center gap-1.5">
                      <CheckIcon className="h-4 w-4 text-[#16a34a]" />
                      Chính sách đã được phê duyệt ({selectedDoc.parent_conformance.toUpperCase()})
                    </span>
                    <span className="text-[11px] text-[#4b5563]">
                      Thời điểm: {new Date(selectedDoc.last_review.ts).toLocaleString("vi-VN")}
                    </span>
                  </div>
                  <p className="mt-1">
                    <b>Người duyệt:</b> {selectedDoc.last_review.user_id} · <b>Căn cứ:</b> {selectedDoc.last_review.clause || "Theo quy chế chung"}
                  </p>
                  <p className="mt-0.5">
                    <b>Lý do ghi nhận:</b> {selectedDoc.last_review.justification}
                  </p>
                </div>
              )}

              {/* So sánh 2 cột: Cột trái (Văn bản) - Cột phải (Chính sách cấp trên đối chiếu) */}
              <div className="grid grid-cols-2 gap-6">
                {/* Cột 1: Văn bản đang xét */}
                <div className="rounded-2xl border border-[#d8ded8] bg-white p-5 shadow-sm">
                  <div className="mb-4 border-b border-[#e6eae6] pb-3">
                    <span className="rounded bg-[#e8f3ed] px-2 py-0.5 text-[10px] font-bold uppercase text-[#165c3a]">
                      Văn bản cấp cơ sở ({selectedDoc.scope})
                    </span>
                    <h3 className="mt-1 text-base font-bold text-[#1c211d]">
                      {selectedDoc.title}
                    </h3>
                    <div className="mt-1 flex flex-wrap gap-3 text-xs text-[#788078]">
                      <span>Đơn vị: <b>{selectedDoc.owner_unit_name}</b></span>
                      <span>Chủ đề: <b>{selectedDoc.topic || "N/A"}</b></span>
                      <span>Hiệu lực từ: <b>{selectedDoc.valid_from}</b></span>
                    </div>
                  </div>

                  <div className="space-y-4">
                    <h4 className="text-xs font-bold uppercase tracking-wider text-[#536058]">
                      Các điều khoản & nội dung:
                    </h4>
                    {selectedDoc.chunks.map((chk) => (
                      <div
                        key={chk.chunk_id}
                        className="rounded-xl border border-[#ecefec] bg-[#fafbfa] p-3.5 text-xs leading-relaxed text-[#2c332e]"
                      >
                        {chk.section_path && (
                          <p className="mb-1.5 font-bold text-[#165c3a]">
                            {chk.section_path}
                          </p>
                        )}
                        <p className="whitespace-pre-line">{chk.content}</p>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Cột 2: Chính sách cấp trên liên quan */}
                <div className="rounded-2xl border border-[#d8ded8] bg-white p-5 shadow-sm">
                  <div className="mb-4 border-b border-[#e6eae6] pb-3">
                    <span className="rounded bg-[#fef3c7] px-2 py-0.5 text-[10px] font-bold uppercase text-[#92400e]">
                      Chính sách cấp trên đối chiếu ({selectedDoc.parent_policy?.scope || "Tập đoàn"})
                    </span>
                    {selectedDoc.parent_policy ? (
                      <>
                        <h3 className="mt-1 text-base font-bold text-[#1c211d]">
                          {selectedDoc.parent_policy.title}
                        </h3>
                        <div className="mt-1 flex flex-wrap gap-3 text-xs text-[#788078]">
                          <span>Ban hành: <b>{selectedDoc.parent_policy.owner_unit_name}</b></span>
                          <span>Bắt buộc: <b>{selectedDoc.parent_policy.mandatory ? "Có (Mandatory)" : "Không"}</b></span>
                        </div>
                      </>
                    ) : (
                      <h3 className="mt-1 text-base font-semibold text-[#788078]">
                        Chưa tìm thấy quy định cấp trên trực tiếp cho chủ đề này.
                      </h3>
                    )}
                  </div>

                  {selectedDoc.parent_policy ? (
                    <div className="space-y-4">
                      <h4 className="text-xs font-bold uppercase tracking-wider text-[#536058]">
                        Điều khoản trần & nguyên tắc khung:
                      </h4>
                      {selectedDoc.parent_policy.chunks.map((chk) => (
                        <div
                          key={chk.chunk_id}
                          className="rounded-xl border border-[#f5ebd7] bg-[#fdfaf3] p-3.5 text-xs leading-relaxed text-[#2c332e]"
                        >
                          {chk.section_path && (
                            <p className="mb-1.5 font-bold text-[#92400e]">
                              {chk.section_path}
                            </p>
                          )}
                          <p className="whitespace-pre-line">{chk.content}</p>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="p-8 text-center text-xs text-[#788078]">
                      Không có văn bản đối chiếu tự động. Owner kiểm tra độc lập theo quy chế hiện hành.
                    </div>
                  )}
                </div>
              </div>
            </div>

            {/* Cột Action Form bên phải */}
            <div className="w-84 shrink-0 border-l border-[#e2e6e2] bg-white p-5 overflow-y-auto">
              <h3 className="text-sm font-bold text-[#1c211d] mb-1">
                {selectedDoc.last_review ? "Cập nhật / Xem lại kết luận" : "Kết luận của Knowledge Owner"}
              </h3>
              <p className="text-[11px] text-[#788078] mb-5">
                Lưu vào Audit Log và đồng bộ hóa quyền truy xuất RAG ngay lập tức.
              </p>

              <form onSubmit={handleSubmit} className="space-y-4">
                <div>
                  <label className="block text-xs font-bold text-[#3c453e] mb-1.5">
                    Trạng thái xác nhận (Conformance)
                  </label>
                  <div className="space-y-2">
                    <label className="flex items-center gap-2 rounded-lg border border-[#d8ded8] p-2.5 text-xs cursor-pointer hover:bg-[#fafbfa]">
                      <input
                        type="radio"
                        name="decision"
                        value="confirmed"
                        checked={decision === "confirmed"}
                        onChange={() => setDecision("confirmed")}
                        className="text-[#165c3a] focus:ring-[#165c3a]"
                      />
                      <div>
                        <span className="font-bold text-[#165c3a]">Confirmed (Hợp chuẩn)</span>
                        <p className="text-[10px] text-[#788078]">Phù hợp chính sách cấp trên, cho phép RAG</p>
                      </div>
                    </label>

                    <label className="flex items-center gap-2 rounded-lg border border-[#d8ded8] p-2.5 text-xs cursor-pointer hover:bg-[#fafbfa]">
                      <input
                        type="radio"
                        name="decision"
                        value="conflicting"
                        checked={decision === "conflicting"}
                        onChange={() => setDecision("conflicting")}
                        className="text-[#991b1b] focus:ring-[#991b1b]"
                      />
                      <div>
                        <span className="font-bold text-[#991b1b]">Conflicting (Xung đột)</span>
                        <p className="text-[10px] text-[#788078]">Vượt trần hoặc trái quy chế khung</p>
                      </div>
                    </label>

                    <label className="flex items-center gap-2 rounded-lg border border-[#d8ded8] p-2.5 text-xs cursor-pointer hover:bg-[#fafbfa]">
                      <input
                        type="radio"
                        name="decision"
                        value="unverified"
                        checked={decision === "unverified"}
                        onChange={() => setDecision("unverified")}
                        className="text-[#788078] focus:ring-[#788078]"
                      />
                      <div>
                        <span className="font-bold text-[#4b5563]">Giữ Unverified</span>
                        <p className="text-[10px] text-[#788078]">Chờ bổ sung giải trình</p>
                      </div>
                    </label>
                  </div>
                </div>

                <div>
                  <label className="block text-xs font-bold text-[#3c453e] mb-1">
                    Căn cứ pháp lý / Điều khoản trích dẫn
                  </label>
                  <input
                    type="text"
                    placeholder="VD: Điều 1 Khoản 2 Quy định Tập đoàn"
                    value={clause}
                    onChange={(e) => setClause(e.target.value)}
                    className="w-full rounded-lg border border-[#d8ded8] bg-[#fafbfa] p-2 text-xs outline-none focus:border-[#165c3a]"
                  />
                </div>

                <div>
                  <label className="block text-xs font-bold text-[#3c453e] mb-1">
                    Lý do & giải trình kết luận <span className="text-[#991b1b]">*</span>
                  </label>
                  <textarea
                    rows={4}
                    placeholder="Ghi rõ lý do xác nhận hoặc căn cứ xác định mâu thuẫn chính sách..."
                    value={justification}
                    onChange={(e) => setJustification(e.target.value)}
                    className="w-full rounded-lg border border-[#d8ded8] bg-[#fafbfa] p-2 text-xs outline-none focus:border-[#165c3a]"
                    required
                  />
                </div>

                <button
                  type="submit"
                  disabled={submitting}
                  className="w-full flex items-center justify-center gap-1.5 rounded-xl bg-[#165c3a] py-2.5 text-xs font-bold text-white shadow hover:bg-[#0f4e30] disabled:opacity-60 transition"
                >
                  {submitting ? "Đang lưu..." : selectedDoc.last_review ? "Cập nhật kết luận & Ghi Audit" : "Phê duyệt & Lưu Audit"}
                  {!submitting && <ArrowRightIcon className="h-3.5 w-3.5" />}
                </button>
              </form>
            </div>
          </main>
        ) : (
          <div className="flex-1 flex items-center justify-center text-xs text-[#788078]">
            Chọn một văn bản từ danh sách bên trái để xem nội dung hoặc kết luận phê duyệt.
          </div>
        )}
      </div>
    </div>
  );
}
