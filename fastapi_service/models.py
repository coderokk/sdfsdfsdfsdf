from pydantic import BaseModel, HttpUrl, Field, validator
from typing import Optional, Dict, Any, List
import uuid
from datetime import datetime

# --- API Request Models ---

class ProcessLinkWebhookRequest(BaseModel):
    url: HttpUrl = Field(..., description="URL to be processed by the Telegram bot.")
    webhook_url: Optional[HttpUrl] = Field(None, description="URL to send asynchronous notifications to.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Optional client metadata to be echoed back in webhooks.")

    @validator('metadata', pre=True, always=True)
    def ensure_metadata_is_dict(cls, v):
        if v is None:
            return {}
        return v

class GetLinkRequestParams(BaseModel):
    url: HttpUrl = Field(..., description="URL to be processed by the Telegram bot.")
    redirect: bool = Field(False, description="If true, redirect to the main file URL instead of returning JSON.")


# --- API Response Models ---

class BaseResponse(BaseModel):
    request_id: str = Field(..., description="Unique ID for this request/task.")

class TaskAcceptedResponse(BaseResponse):
    message: str = "Request accepted for asynchronous processing."
    task_id: str = Field(..., description="Unique ID assigned to this asynchronous task.")

class SynchronousLinkProcessResponse(BaseResponse):
    original_url: HttpUrl
    main_file_url: Optional[str] = Field(None, description="Public URL or S3 key of the main file.")
    main_file_original_name: Optional[str] = Field(None, description="Original human-readable name of the main file.")
    main_file_s3_key: Optional[str] = Field(None, description="S3 key of the main file.")
    main_file_size_bytes: Optional[int] = Field(None, description="Size of the main file in bytes.")
    license_file_url: Optional[str] = Field(None, description="Public URL or S3 key of the license file.")
    license_file_original_name: Optional[str] = Field(None, description="Original human-readable name of the license file.")
    license_file_s3_key: Optional[str] = Field(None, description="S3 key of the license file.")
    license_file_size_bytes: Optional[int] = Field(None, description="Size of the license file in bytes.")
    processed_by_account: Optional[str] = Field(None, description="Name of the Telegram account that processed the request.")
    processed_by_phone_number: Optional[str] = Field(None, description="Phone number of the Telegram account.")
    error: Optional[str] = Field(None, description="Error message if processing failed.")
    telegram_error_type: Optional[str] = Field(None, description="Specific type of error from Telegram interaction.")

class ErrorResponse(BaseModel):
    detail: str

class HealthResponse(BaseModel):
    app_version: str
    service_status: str # "ok", "warning", "error"
    message: Optional[str] = None
    active_clients: int
    cooldown_clients_count: int
    flood_wait_clients_count: int
    error_clients_count: int
    auth_error_clients_count: int
    deactivated_clients_count: int
    expired_clients_count: int
    timeout_clients_count: int
    other_status_clients_count: int
    tasks_waiting_for_client: int
    total_configured_clients: int
    s3_configured: bool
    s3_public_base_url_configured: bool
    daily_request_limit_per_session: int
    clients_at_daily_limit_today: int
    clients_statuses_detailed: Dict[str, str] # "Account Name (phone, SID)" -> "status (today: X/Y)"

class StatsAccountDetail(BaseModel):
    name: Optional[str] = None
    total_uses: int = 0
    last_active: Optional[str] = None # ISO timestamp
    status_from_worker: Optional[str] = "unknown"
    session_string_ref: Optional[str] = None
    daily_usage: Dict[str, int] = Field(default_factory=dict) # "YYYY-MM-DD_utc": count
    notified_daily_limit_today: Optional[bool] = False # Renamed for clarity
    notified_error: Optional[bool] = False

class StatsFileContent(BaseModel):
    retrieved_at_utc: datetime
    data_source_file: str
    data: Dict[str, StatsAccountDetail]


# --- Internal Data Structures (not directly for API, but for DB/State) ---

class WebhookTask(BaseModel):
    task_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    original_url: HttpUrl
    webhook_url: Optional[HttpUrl] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    status: str = "pending_link_retrieval" # e.g., pending_link_retrieval, processing_link_retrieval, links_retrieved_pending_s3_upload, processing_s3_upload, completed, failed_XYZ, waiting_for_client
    added_at: datetime = Field(default_factory=datetime.utcnow)
    status_updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    
    # Results from Telegram
    main_download_url_from_bot: Optional[HttpUrl] = None # URL received from bot
    license_download_url_from_bot: Optional[HttpUrl] = None # URL received from bot
    
    # S3 Upload results
    s3_main_file_key: Optional[str] = None
    s3_main_file_original_name: Optional[str] = None
    s3_main_file_size_bytes: Optional[int] = None
    s3_license_file_key: Optional[str] = None
    s3_license_file_original_name: Optional[str] = None
    s3_license_file_size_bytes: Optional[int] = None

    processed_by_account: Optional[str] = None
    processed_by_phone_number: Optional[str] = None
    
    error_details: Optional[str] = None
    error_type: Optional[str] = None # e.g., NoClientAvailable, TelegramError, DownloadFailed, UploadFailed
    
    webhook_status: str = "pending_send" # pending_send, sent, failed_after_retries, not_configured
    webhook_error: Optional[str] = None
    webhook_last_attempt_at: Optional[datetime] = None
    webhook_retry_count: int = 0

    client_request_id: Optional[str] = None # For idempotency, extracted from metadata if present

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None,
            HttpUrl: lambda v: str(v) if v else None,
        }
        # For Pydantic v2:
        # from pydantic_core import core_schema
        # @classmethod
        # def __get_pydantic_json_schema__(cls, core_schema, handler):
        #     json_schema = handler(core_schema)
        #     json_schema = handler.resolve_ref_schema(json_schema)
        #     if "properties" in json_schema:
        #         if "webhook_url" in json_schema["properties"] and json_schema["properties"]["webhook_url"].get("type") == "string":
        #             json_schema["properties"]["webhook_url"]["format"] = "uri"
        #         if "original_url" in json_schema["properties"] and json_schema["properties"]["original_url"].get("type") == "string":
        #             json_schema["properties"]["original_url"]["format"] = "uri"
        #         # Add for other HttpUrl fields if needed
        #     return json_schema


class TelegramClientDetails(BaseModel):
    phone: str
    name: str
    original_phone_hint: str # The phone number as initially provided in sessions.json

# --- Webhook Payloads ---
class WebhookPayloadBase(BaseModel):
    task_id: str
    original_url: HttpUrl
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Optional[Dict[str, Any]] = None
    processed_by_phone_number: Optional[str] = None

class WebhookSuccessPayload(WebhookPayloadBase):
    status: str = "success"
    main_file_url: Optional[str] = None # Full public URL or S3 key
    main_file_original_name: Optional[str] = None
    main_file_s3_key: Optional[str] = None
    main_file_size_bytes: Optional[int] = None
    license_file_url: Optional[str] = None
    license_file_original_name: Optional[str] = None
    license_file_s3_key: Optional[str] = None
    license_file_size_bytes: Optional[int] = None

class WebhookErrorPayload(WebhookPayloadBase):
    status: str = "error"
    error_message: str
    error_type: Optional[str] = None # e.g., NoClientAvailable, TelegramError, DownloadFailed

class WebhookProcessingUpdatePayload(WebhookPayloadBase):
    status: str # e.g., "processing_started", "links_retrieved", "retrying_no_client"
    message: Optional[str] = None


# --- For parsing S3 response headers ---
class ContentDisposition(BaseModel):
    filename: Optional[str] = None
    filename_star: Optional[str] = None # For filename*=UTF-8''...