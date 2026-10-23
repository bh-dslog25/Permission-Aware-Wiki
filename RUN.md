# Hướng dẫn chạy dự án Permission-Aware RAG Wiki

## 1. Yêu cầu

- Docker Desktop đã cài và đang chạy
- Node.js 18+ hoặc 20+
- Python 3.11+
- Git

## 2. Khởi động hạ tầng Docker

Tại thư mục gốc của dự án:

```bash
docker compose up -d
```

Kiểm tra trạng thái:

```bash
docker compose ps
```

Bạn nên thấy các service sau đang chạy:
- `agroup-postgres`
- `agroup-openfga`
- `agroup-openfga-migrate` (đã exit thành công sau khi migrate)

Các port mặc định:
- PostgreSQL: `5432`
- OpenFGA HTTP: `8080`
- OpenFGA gRPC: `8081`
- OpenFGA Playground: `3000`

## 3. Cấu hình môi trường backend

Vào thư mục `backend`:

```bash
cd backend
python -m venv .venv
```

Trên Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Trên macOS/Linux:

```bash
source .venv/bin/activate
```

Cài đặt dependencies theo file `pyproject.toml`:

```bash
pip install -e .
```

Nếu cần dùng các package cho ingest/embedding ML, cài thêm extra:

```bash
pip install -e ".[ml]"
```

Sau đó kiểm tra file `.env` hoặc file cấu hình backend để đảm bảo khớp với Docker DB.

Khởi tạo/cập nhật schema (lệnh này idempotent, không xóa dữ liệu hiện có):

```powershell
python -m app.db init
```

Chạy lệnh này sau khi cập nhật backend để tạo bảng lịch sử hội thoại nếu database đã tồn tại.

## 4. Chạy backend

```bash
cd backend
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Backend thường chạy tại:
- `http://localhost:8000`
- Swagger UI: `http://localhost:8000/docs`

## 5. Chạy frontend

Mở terminal mới:

```bash
cd frontend
npm install
npm run dev
```

Frontend thường chạy tại:
- `http://localhost:8443`

## 6. Kiểm tra nhanh

- Backend API: `http://localhost:8000/docs`
- OpenFGA Playground: `http://localhost:3000/playground`
- Frontend: `http://localhost:8443`

## 7. Nếu cần reset Docker

```bash
docker compose down -v
docker compose up -d
```

## 8. Ghi chú

- Nếu PostgreSQL khởi động lỗi do volume cũ, nên xóa volume bằng `docker compose down -v`.
- Nếu OpenFGA báo database không tồn tại, hãy đảm bảo Docker đã bắt đầu đầy đủ và script init đã chạy.
- Nếu backend báo thiếu dependency, cài lại bằng `pip install -e .` hoặc `pip install -e ".[ml]"` nếu cần chạy ingest/embedding.

## 9. Các lệnh dùng thường xuyên

```bash
docker compose up -d
docker compose ps
docker compose logs -f
```

```bash
..\backend
.\.venv\Scripts\python.exe -m pip install -e ".[ml]"
.\.venv\Scripts\python.exe -m app.ingest.ingest run --force
```

```bash
cd backend
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

```bash
cd frontend
npm run dev
```
