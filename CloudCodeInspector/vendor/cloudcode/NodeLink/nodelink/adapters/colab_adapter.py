from typing import Dict, Any, Optional
from .generic_rest import GenericRestConnector
from ..models import ApiResponse, RemoteJobHandle, JobResult

class ColabAdapter(GenericRestConnector):
    """
    Adapter for Google Colab or remote Jupyter runtimes.
    Provides tunneling and execution hooks for remote runtime sessions.
    """
    
    async def run_remote(self, command_or_notebook: str, params: Optional[Dict] = None) -> RemoteJobHandle:
        """
        Executes code on a remote Jupyter kernel.
        Usually requires interacting with Jupyter REST API (e.g., /api/sessions, /api/kernels)
        """
        # Pseudo-implementation: submit cell execution
        payload = {
            "code": command_or_notebook,
            "silent": False
        }
        
        # Assuming we know the kernel_id via params
        kernel_id = params.get("kernel_id", "default_kernel")
        endpoint = f"api/kernels/{kernel_id}/execute"
        
        res = await self.send_request(endpoint, method="POST", data=payload)
        
        if res.status_code in [200, 202]:
            msg_id = res.data.get("msg_id", "unknown_msg")
            return RemoteJobHandle(target_id=self.target_id, job_id=msg_id, status="EXECUTING")
            
        raise RuntimeError(f"Failed to execute on Colab/Jupyter: {res.data}")

    async def fetch_results(self, job_handle: RemoteJobHandle, download_path: Optional[str] = None) -> JobResult:
        """
        Polls for Jupyter execution results.
        """
        # In a real Jupyter environment, results often come through WebSockets.
        # Here we mock a REST status check.
        res = await self.send_request(f"api/messages/{job_handle.job_id}", method="GET")
        
        if res.status_code == 200:
            return JobResult(
                job_id=job_handle.job_id, 
                status="COMPLETED",
                result_data=res.data
            )
            
        return JobResult(job_id=job_handle.job_id, status="ERROR", error=str(res.data))
