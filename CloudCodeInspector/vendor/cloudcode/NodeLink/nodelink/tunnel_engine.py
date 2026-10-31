import asyncio
import websockets
from datetime import datetime
from typing import Dict, Callable, Optional, Any
from .nodelog_stub import NodeLog

class TunnelState:
    CONNECTING = "CONNECTING"
    ESTABLISHED = "ESTABLISHED"
    RECONNECTING = "RECONNECTING"
    TERMINATED = "TERMINATED"

class TunnelEngine:
    """
    Manages bi-directional tunneling (via WebSocket) to route remote signals.
    Provides health-check heartbeats and auto-reconnection logic.
    """
    def __init__(self, target_id: str, endpoint: str, headers: Optional[dict] = None):
        self.target_id = target_id
        self.endpoint = endpoint
        self.headers = headers or {}
        
        self.state = TunnelState.TERMINATED
        self.websocket = None
        self.last_heartbeat = None
        self._shutdown_event = asyncio.Event()
        self._message_handlers = []
        self._reconnect_delay = 2

    def register_handler(self, handler: Callable[[str], Any]):
        """Registers a callback for incoming messages."""
        self._message_handlers.append(handler)

    async def connect(self):
        """Initiates the tunnel connection."""
        self.state = TunnelState.CONNECTING
        self._shutdown_event.clear()
        
        while not self._shutdown_event.is_set():
            try:
                NodeLog.info(f"Connecting tunnel to {self.endpoint} for {self.target_id}")
                # Convert http(s) to ws(s) if necessary, simplified for this module
                ws_url = self.endpoint.replace("http", "ws") 
                
                async with websockets.connect(ws_url, additional_headers=self.headers) as ws:
                    self.websocket = ws
                    self.state = TunnelState.ESTABLISHED
                    self.last_heartbeat = datetime.utcnow()
                    self._reconnect_delay = 2 # reset delay
                    
                    NodeLog.audit("TUNNEL_ESTABLISHED", {"target_id": self.target_id})
                    
                    # Run listening and heartbeat loops concurrently
                    listen_task = asyncio.create_task(self._listen_loop())
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                    
                    done, pending = await asyncio.wait(
                        [listen_task, heartbeat_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    for task in pending:
                        task.cancel()
                        
            except Exception as e:
                NodeLog.warn(f"Tunnel connection error for {self.target_id}: {e}")
                
            if not self._shutdown_event.is_set():
                self.state = TunnelState.RECONNECTING
                NodeLog.info(f"Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _listen_loop(self):
        try:
            async for message in self.websocket:
                self.last_heartbeat = datetime.utcnow()
                for handler in self._message_handlers:
                    # In a real app, handlers might be async
                    if asyncio.iscoroutinefunction(handler):
                        await handler(message)
                    else:
                        handler(message)
        except websockets.exceptions.ConnectionClosed:
            NodeLog.warn(f"Tunnel closed for {self.target_id}")

    async def _heartbeat_loop(self):
        while self.state == TunnelState.ESTABLISHED and not self._shutdown_event.is_set():
            await asyncio.sleep(30) # ping every 30s
            try:
                await self.websocket.ping()
                self.last_heartbeat = datetime.utcnow()
            except Exception:
                NodeLog.warn(f"Heartbeat failed for {self.target_id}")
                break

    async def send(self, data: str):
        """Sends data through the tunnel."""
        if self.state == TunnelState.ESTABLISHED and self.websocket:
            await self.websocket.send(data)
        else:
            raise RuntimeError(f"Cannot send, tunnel is in state {self.state}")

    async def disconnect(self):
        """Terminates the tunnel gracefully."""
        self._shutdown_event.set()
        self.state = TunnelState.TERMINATED
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
        NodeLog.audit("TUNNEL_TERMINATED", {"target_id": self.target_id})
