import asyncio
from typing import Dict, Any, Optional

from .models import ConnectionHandle, ApiResponse, RemoteJobHandle, JobResult, ConnectionState
from .auth_manager import AuthManager
from .sync_manager import SyncManager
from .tunnel_engine import TunnelEngine
from .nodelog_stub import NodeLog
from .adapters.base_adapter import BaseAdapter
from .adapters.generic_rest import GenericRestConnector
from .adapters.kaggle_adapter import KaggleAdapter
from .adapters.colab_adapter import ColabAdapter

class NodeLink:
    """
    Unified Gateway for the NodeLink module.
    Orchestrates authentication, adapters, tunneling, and syncing.
    """
    def __init__(self, global_config: Optional[Dict[str, Any]] = None):
        self.auth_manager = AuthManager(global_config)
        self.sync_manager = SyncManager()
        
        # Internal state
        self._connections: Dict[str, ConnectionState] = {}
        self._adapters: Dict[str, BaseAdapter] = {}
        self._tunnels: Dict[str, TunnelEngine] = {}

    def _create_adapter(self, target_id: str, adapter_type: str, endpoint: str, headers: Dict[str, str]) -> BaseAdapter:
        adapter_type = adapter_type.lower()
        if adapter_type == "kaggle":
            return KaggleAdapter(target_id, headers)
        elif adapter_type in ["colab", "jupyter"]:
            # Colab adapter could override endpoint
            colab = ColabAdapter(target_id, endpoint, headers)
            return colab
        else:
            return GenericRestConnector(target_id, endpoint, headers)

    def connect(self, target_id: str, config: Dict[str, Any]) -> ConnectionHandle:
        """
        Establishes a logical connection/configuration for a target.
        If tunnel=True, starts a WebSocket tunnel.
        """
        endpoint = config.get("endpoint", "")
        auth_type = config.get("auth_type", "NONE")
        adapter_type = config.get("adapter", "generic")
        
        # 1. Resolve Auth Headers
        headers = self.auth_manager.get_auth_headers(target_id, config)
        self.auth_manager.log_auth_event(target_id, "CONNECT_INIT", {"config": config, "headers": headers})
        
        # 2. Setup Adapter
        adapter = self._create_adapter(target_id, adapter_type, endpoint, headers)
        self._adapters[target_id] = adapter
        
        # 3. Register Connection State
        state = ConnectionState(
            target_id=target_id,
            status="ESTABLISHED",
            endpoint=endpoint,
            auth_type=auth_type
        )
        self._connections[target_id] = state
        
        # 4. Optional Tunnel setup
        if config.get("tunnel", False):
            tunnel = TunnelEngine(target_id, endpoint, headers)
            self._tunnels[target_id] = tunnel
            # Start tunnel in background
            asyncio.create_task(tunnel.connect())
            
        NodeLog.audit("CONNECTION_ESTABLISHED", {"target_id": target_id, "adapter": adapter_type})
        return state.to_handle()

    async def send_request(self, target_id: str, endpoint: str, method: str = "POST", 
                           headers: Optional[Dict[str, str]] = None, 
                           data: Optional[Dict[str, Any]] = None, 
                           files: Optional[list] = None) -> ApiResponse:
        """Sends a request via the target's adapter."""
        if target_id not in self._adapters:
            raise ValueError(f"No active connection for target: {target_id}")
            
        adapter = self._adapters[target_id]
        
        # Optionally merge ad-hoc headers
        if headers:
            # We don't mutate the adapter's base headers, but pass them for this request.
            # (In a full implementation, we'd pass these merged headers down to the adapter)
            pass
            
        res = await adapter.send_request(endpoint, method, data, files)
        
        # Update metrics
        if target_id in self._connections:
            # Rough approximation
            self._connections[target_id].bytes_sent += len(str(data)) if data else 0
            self._connections[target_id].bytes_received += len(str(res.data)) if res.data else 0
            
        return res

    async def run_remote(self, target_id: str, command_or_notebook: str, params: Optional[Dict] = None) -> RemoteJobHandle:
        """Triggers remote execution via the adapter."""
        if target_id not in self._adapters:
            raise ValueError(f"No active connection for target: {target_id}")
            
        return await self._adapters[target_id].run_remote(command_or_notebook, params)

    async def fetch_results(self, job_handle: RemoteJobHandle, download_path: Optional[str] = None) -> JobResult:
        """Fetches status or results for a remote job."""
        target_id = job_handle.target_id
        if target_id not in self._adapters:
            raise ValueError(f"No active connection for target: {target_id}")
            
        return await self._adapters[target_id].fetch_results(job_handle, download_path)

    async def disconnect(self, target_id: str) -> bool:
        """Tears down connections and tunnels for a target."""
        if target_id in self._tunnels:
            await self._tunnels[target_id].disconnect()
            del self._tunnels[target_id]
            
        if target_id in self._adapters:
            del self._adapters[target_id]
            
        if target_id in self._connections:
            self._connections[target_id].status = "TERMINATED"
            NodeLog.audit("CONNECTION_TERMINATED", {"target_id": target_id})
            del self._connections[target_id]
            
        return True
