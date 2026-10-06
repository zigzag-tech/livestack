Resolution order for a minted grant's host: principal.upload_base_url, else server public_base_url, else request Host. The public
route itself (relay) is unchanged. The guard lives in `check_grant_origins`, called at startup (a reload never changes the global value).
