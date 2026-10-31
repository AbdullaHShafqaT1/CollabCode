import hashlib
import aiohttp
import os
from typing import Optional
from .nodelog_stub import NodeLog

class SyncManager:
    """
    Handles high-performance chunked uploading/downloading of artifacts
    and checksum validation (SHA-256).
    """
    CHUNK_SIZE = 1024 * 1024 * 5  # 5MB chunks

    @staticmethod
    def calculate_checksum(file_path: str) -> str:
        """Calculates SHA-256 checksum of a file."""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()

    async def download_file(self, url: str, destination_path: str, headers: Optional[dict] = None) -> bool:
        """
        Downloads a file in chunks asynchronously.
        """
        NodeLog.info(f"Starting download from {url} to {destination_path}")
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    
                    # Create directory if it doesn't exist
                    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
                    
                    with open(destination_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(self.CHUNK_SIZE):
                            f.write(chunk)
                            
            checksum = self.calculate_checksum(destination_path)
            NodeLog.audit("DOWNLOAD_COMPLETE", {"url": url, "path": destination_path, "sha256": checksum})
            return True
        except Exception as e:
            NodeLog.error(f"Download failed: {e}")
            return False

    async def upload_file(self, url: str, file_path: str, headers: Optional[dict] = None) -> bool:
        """
        Uploads a file in chunks asynchronously.
        """
        if not os.path.exists(file_path):
            NodeLog.error(f"File not found: {file_path}")
            return False
            
        checksum = self.calculate_checksum(file_path)
        NodeLog.info(f"Starting upload of {file_path} (SHA256: {checksum}) to {url}")
        
        try:
            # We can use aiohttp's built-in file streaming
            async with aiohttp.ClientSession(headers=headers) as session:
                with open(file_path, 'rb') as f:
                    async with session.post(url, data=f) as response:
                        response.raise_for_status()
                        
            NodeLog.audit("UPLOAD_COMPLETE", {"url": url, "path": file_path, "sha256": checksum})
            return True
        except Exception as e:
            NodeLog.error(f"Upload failed: {e}")
            return False
