from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from ..models import ApiResponse, RemoteJobHandle, JobResult

class BaseAdapter(ABC):
    """
    Abstract base class for all NodeLink adapters.
    """
    def __init__(self, target_id: str, base_url: str, headers: Dict[str, str]):
        self.target_id = target_id
        self.base_url = base_url
        self.headers = headers

    @abstractmethod
    async def send_request(self, endpoint: str, method: str = "POST", 
                           data: Optional[Dict] = None, files: Optional[list] = None) -> ApiResponse:
        pass

    @abstractmethod
    async def run_remote(self, command_or_notebook: str, params: Optional[Dict] = None) -> RemoteJobHandle:
        pass

    @abstractmethod
    async def fetch_results(self, job_handle: RemoteJobHandle, download_path: Optional[str] = None) -> JobResult:
        pass
