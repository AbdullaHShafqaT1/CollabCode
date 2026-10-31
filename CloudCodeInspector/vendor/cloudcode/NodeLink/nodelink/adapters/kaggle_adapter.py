from typing import Dict, Any, Optional
from .generic_rest import GenericRestConnector
from ..models import ApiResponse, RemoteJobHandle, JobResult

class KaggleAdapter(GenericRestConnector):
    """
    Adapter for Kaggle REST APIs.
    Supports dataset uploads/downloads, kernel pushes, notebook execution status checks.
    """
    
    def __init__(self, target_id: str, headers: Dict[str, str]):
        # Base URL for Kaggle API v1
        super().__init__(target_id, "https://www.kaggle.com/api/v1", headers)

    async def run_remote(self, command_or_notebook: str, params: Optional[Dict] = None) -> RemoteJobHandle:
        """
        Pushes and runs a Kaggle kernel.
        `command_or_notebook` could be a local path to kernel-metadata.json or similar payload.
        """
        # For simplicity, assuming `params` contains the required payload for /kernels/push
        payload = params or {}
        res = await self.send_request("kernels/push", method="POST", data=payload)
        
        if res.status_code == 200:
            # Kaggle responds with a ref like "username/kernel-name"
            job_id = res.data.get("ref", "unknown_kernel_ref")
            return RemoteJobHandle(target_id=self.target_id, job_id=job_id, status="SUBMITTED")
        raise RuntimeError(f"Failed to push Kaggle kernel: {res.data}")

    async def fetch_results(self, job_handle: RemoteJobHandle, download_path: Optional[str] = None) -> JobResult:
        """
        Checks kernel status and optionally triggers a download if finished.
        """
        # kernel status check uses username/kernel-name ref
        username, kernel_slug = job_handle.job_id.split('/', 1) if '/' in job_handle.job_id else ("unknown", job_handle.job_id)
        
        res = await self.send_request(f"kernels/status/{username}/{kernel_slug}", method="GET")
        
        if res.status_code == 200:
            status = res.data.get("status", "UNKNOWN")
            
            # If finished, we could use SyncManager to download output, 
            # but for the interface we just return the status.
            return JobResult(
                job_id=job_handle.job_id, 
                status=status,
                result_data=res.data
            )
            
        return JobResult(job_id=job_handle.job_id, status="ERROR", error=str(res.data))
