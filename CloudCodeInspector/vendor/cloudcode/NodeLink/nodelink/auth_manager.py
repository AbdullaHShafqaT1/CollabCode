import os
from typing import Dict, Any, Optional
from .nodelog_stub import NodeLog

class AuthManager:
    """
    Manages authentication credentials, token injection, and masking for safe logging.
    Supports environment variables and configuration-based credentials.
    """
    def __init__(self, global_config: Optional[Dict[str, Any]] = None):
        self.global_config = global_config or {}
        
    def get_auth_headers(self, target_id: str, config: Dict[str, Any]) -> Dict[str, str]:
        """
        Constructs authorization headers based on config or environment variables.
        """
        headers = {}
        auth_type = config.get("auth_type", "NONE").upper()
        
        if auth_type == "BEARER_TOKEN":
            token = self._resolve_credential(target_id, "token", config)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "API_KEY":
            api_key = self._resolve_credential(target_id, "api_key", config)
            if api_key:
                # Some APIs might use different header names for API keys
                header_name = config.get("api_key_header", "x-api-key")
                headers[header_name] = api_key
        elif auth_type == "BASIC":
            username = self._resolve_credential(target_id, "username", config)
            password = self._resolve_credential(target_id, "password", config)
            if username and password:
                import base64
                encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
                headers["Authorization"] = f"Basic {encoded}"
                
        return headers

    def _resolve_credential(self, target_id: str, cred_key: str, config: Dict[str, Any]) -> Optional[str]:
        """
        Resolves a credential by checking the provided config, then environment variables.
        """
        # Check explicit config first
        if cred_key in config:
            return config[cred_key]
            
        # Check global config
        target_config = self.global_config.get(target_id, {})
        if cred_key in target_config:
            return target_config[cred_key]
            
        # Check environment variables: e.g., KAGGLE_TOKEN, TARGETID_API_KEY
        env_var_specific = f"{target_id.upper()}_{cred_key.upper()}"
        if env_var_specific in os.environ:
            return os.environ[env_var_specific]
            
        return None

    def sanitize_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """
        Masks sensitive information in headers for telemetry and logging.
        """
        sanitized = dict(headers)
        sensitive_keys = ['authorization', 'x-api-key', 'token', 'api-key']
        
        for key in sanitized.keys():
            if key.lower() in sensitive_keys:
                val = sanitized[key]
                if len(val) > 10:
                    sanitized[key] = f"{val[:4]}...{val[-4:]}"
                else:
                    sanitized[key] = "***"
        return sanitized
        
    def log_auth_event(self, target_id: str, event_type: str, details: Dict[str, Any]):
        """
        Safely logs auth events using NodeLog.
        """
        safe_details = {}
        for k, v in details.items():
            if k in ['headers', 'config']:
                safe_details[k] = self.sanitize_headers(v) if isinstance(v, dict) else v
            else:
                safe_details[k] = v
                
        NodeLog.audit(event_type, {"target_id": target_id, **safe_details})
