import uuid

from src.models.base import SessionLocal
from src.models.chat_request import ChatRequest
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class ChatRequestService:
    @staticmethod
    def add_chat_request(
        api_key_id: uuid.UUID,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        model_name: str,
        image_count: int = 0,
    ) -> bool:
        """
        Record a chat request.

        Args:
            api_key_id: UUID of the API key used
            input_tokens: Number of input tokens used
            output_tokens: Number of output tokens generated
            cached_tokens: Number of cached tokens used
            model_name: Name of the model used
            image_count: Number of images generated

        Returns:
            Boolean indicating if the operation was successful
        """
        logger.debug(
            f"Recording chat request: model={model_name}, input_tokens={input_tokens}, "
            f"output_tokens={output_tokens}, cached_tokens={cached_tokens}, image_count={image_count}, api_key_id={api_key_id}"
        )

        try:
            with SessionLocal() as db:
                chat_request = ChatRequest(
                    api_key_id=api_key_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    model_name=model_name,
                    image_count=image_count,
                )
                db.add(chat_request)
                db.commit()
                return True
        except Exception as e:
            logger.error(f"Error recording chat request: {str(e)}", exc_info=True)
            raise
