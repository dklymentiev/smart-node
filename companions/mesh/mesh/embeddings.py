#!/usr/bin/env python3
"""
Sentence transformers integration for document embeddings
"""
import logging
import asyncio
from typing import List, Optional
from sentence_transformers import SentenceTransformer
from .config import get_embedding_model_name

logger = logging.getLogger(__name__)

class EmbeddingService:
    """Service for generating document embeddings"""
    
    def __init__(self):
        self.model: Optional[SentenceTransformer] = None
        self.model_name = get_embedding_model_name()
        # Three queue consumers share one model. The lock prevents a cold
        # start from constructing it three times after construction moves off
        # the event-loop thread.
        self._initialize_lock = asyncio.Lock()
        
    # Expected vector dimensions matching database schema vector(768)
    EXPECTED_DIMENSIONS = 768

    async def initialize(self):
        """Initialize the model without blocking the HTTP event loop."""
        if self.model is not None:
            return

        async with self._initialize_lock:
            if self.model is not None:
                return
            try:
                logger.info(f"Loading embedding model: {self.model_name}")
                # SentenceTransformer performs synchronous disk reads and CPU
                # setup. A queue worker used to run this constructor directly
                # on uvicorn's sole event-loop thread, so the first queued PUT
                # could not flush its already-built response for 20+ seconds.
                model = await asyncio.to_thread(
                    SentenceTransformer, self.model_name
                )

                # Validate that model dimensions match database schema before
                # publishing it to the other queue workers.
                dim = model.get_sentence_embedding_dimension()
                if dim != self.EXPECTED_DIMENSIONS:
                    raise RuntimeError(
                        f"Model '{self.model_name}' produces {dim}-dim vectors, "
                        f"but database schema expects vector({self.EXPECTED_DIMENSIONS}). "
                        f"Change EMBEDDING_MODEL or update the schema."
                    )
                self.model = model

                logger.info(
                    f"Embedding model loaded: {self.model_name} ({dim} dimensions)"
                )
            except Exception as e:
                logger.error(f"Failed to load embedding model: {e}")
                raise
    
    def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding for a text string"""
        if not self.model:
            raise RuntimeError("Embedding model not initialized")
        
        if not text.strip():
            raise ValueError("Text cannot be empty")
        
        try:
            # Generate embedding
            embedding = self.model.encode(text, convert_to_numpy=True)
            
            # Convert to list for JSON serialization
            return embedding.tolist()
        except Exception as e:
            logger.error(f"Failed to generate embedding: {e}")
            raise
    
    def generate_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts"""
        if not self.model:
            raise RuntimeError("Embedding model not initialized")
        
        if not texts:
            return []
        
        try:
            # Generate embeddings in batch for efficiency
            embeddings = self.model.encode(texts, convert_to_numpy=True)
            
            # Convert to list of lists
            return [emb.tolist() for emb in embeddings]
        except Exception as e:
            logger.error(f"Failed to generate batch embeddings: {e}")
            raise
    
# Global embedding service instance
embedding_service = EmbeddingService()

async def get_embedding_service() -> EmbeddingService:
    """Get the global embedding service instance"""
    if not embedding_service.model:
        await embedding_service.initialize()
    return embedding_service
