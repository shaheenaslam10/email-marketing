from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List


@dataclass
class SendResult:
    success: bool
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None
    status_code: Optional[int] = None
    is_transient_error: bool = False


class BaseEmailProvider(ABC):
    def __init__(self, sender):
        self.sender = sender

    @abstractmethod
    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
        reply_to: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None,
        log_context: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Sends an email and returns SendResult.
        log_context carries optional diagnostic correlation ids
        (campaign_id/message_id/source); it is only ever logged in
        sanitized form and never sent to the provider.
        """
        pass

    @abstractmethod
    def test_connection(self) -> Tuple[bool, str]:
        """Tests connectivity and credentials for this provider."""
        pass
