"""Names and permissions for the SQL-managed FOMC vector schema."""

STOCK_REVISION = "0008"
VECTOR_REVISION = "0009"
PROFILE_TABLE = "fomc_embedding_profiles"
RUN_TABLE = "fomc_embedding_runs"
REQUEST_TABLE = "fomc_embedding_requests"
ITEM_TABLE = "fomc_embedding_request_items"
EMBEDDING_TABLE = "fomc_passage_embeddings"
VECTOR_TABLES = (PROFILE_TABLE, RUN_TABLE, REQUEST_TABLE, ITEM_TABLE, EMBEDDING_TABLE)
SOURCE_KEY = "uq_fomc_passage_vector_input"
RUN_UPDATES = ("state", "finished_at", "error_code")
REQUEST_UPDATES = (
    "state",
    "dispatch_at",
    "finished_at",
    "provider_request_id",
    "returned_model",
    "prompt_tokens",
    "total_tokens",
    "error_code",
    "reconciled_at",
)
