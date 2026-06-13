import os
import ast
from pathlib import Path
from typing import List, Dict

try:
    from llama_index.core import Document, VectorStoreIndex, Settings
    from llama_index.vector_stores.chroma import ChromaVectorStore
    from llama_index.embeddings.ollama import OllamaEmbedding
    import chromadb
    LLAMA_INDEX_AVAILABLE = True
except ImportError:
    LLAMA_INDEX_AVAILABLE = False

from aegis_sre.telemetry.logger import logger

class ASTCodeSplitter:
    """
    Zero-dependency Python AST parser that logically splits code by 
    Classes, Functions, and module-level code, avoiding arbitrary text chunking.
    """
    def split_file(self, file_path: str, content: str) -> List[Document]:
        documents = []
        try:
            tree = ast.parse(content)
            lines = content.splitlines()
            
            # Module level code chunks
            module_code = []
            
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start_line = node.lineno - 1
                    end_line = node.end_lineno
                    snippet = "\n".join(lines[start_line:end_line])
                    
                    doc = Document(
                        text=snippet,
                        metadata={
                            "file_path": file_path,
                            "type": type(node).__name__,
                            "name": node.name,
                            "start_line": start_line + 1,
                            "end_line": end_line
                        }
                    )
                    documents.append(doc)
                else:
                    # Collect module level expressions (imports, assignments)
                    start = node.lineno - 1
                    end = node.end_lineno
                    module_code.extend(lines[start:end])
            
            if module_code:
                doc = Document(
                    text="\n".join(module_code),
                    metadata={
                        "file_path": file_path,
                        "type": "ModuleLevel",
                        "name": "globals",
                    }
                )
                documents.append(doc)
                
        except SyntaxError as e:
            logger.warning("ast_parse_error", file_path=file_path, error=str(e))
            # Fallback to simple chunking if syntax error exists
            doc = Document(text=content, metadata={"file_path": file_path, "type": "raw"})
            documents.append(doc)
            
        return documents

class RAGEngine:
    def __init__(self, workspace_path: str = "."):
        self.workspace_path = workspace_path
        self.code_index = None
        self.skills_index = None
        self.splitter = ASTCodeSplitter()
        
        if LLAMA_INDEX_AVAILABLE:
            try:
                # Use local Ollama embeddings to preserve Service Provider Proof status
                ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
                Settings.embed_model = OllamaEmbedding(
                    model_name="nomic-embed-text",
                    base_url=ollama_url
                )
                
                # Setup local ChromaDB with Dual Collections
                db = chromadb.PersistentClient(path=os.path.join(workspace_path, ".chroma_db"))
                
                code_collection = db.get_or_create_collection("aegis_codebase")
                self.code_store = ChromaVectorStore(chroma_collection=code_collection)
                
                skills_collection = db.get_or_create_collection("aegis_skills")
                self.skills_store = ChromaVectorStore(chroma_collection=skills_collection)
                
            except Exception as e:
                logger.error("rag_engine_init_failed", error=str(e))
                self.code_store = None
                self.skills_store = None
        else:
            logger.warning("llama_index_not_installed", module="rag_engine")
            
    def ingest_workspace(self):
        """Crawls the local workspace and builds the AST RAG index."""
        if not LLAMA_INDEX_AVAILABLE or not self.code_store:
            return
            
        logger.info("starting_workspace_ingestion", path=self.workspace_path)
        documents = []
        
        # Crawl python files
        for root, _, files in os.walk(self.workspace_path):
            if "venv" in root or ".chroma_db" in root or "__pycache__" in root:
                continue
                
            for file in files:
                if file.endswith(".py"):
                    full_path = os.path.join(root, file)
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        
                        docs = self.splitter.split_file(full_path, content)
                        documents.extend(docs)
                    except Exception as e:
                        logger.error("file_ingest_error", file=full_path, error=str(e))
                        
        if documents:
            try:
                from llama_index.core import StorageContext
                storage_context = StorageContext.from_defaults(vector_store=self.code_store)
                self.code_index = VectorStoreIndex.from_documents(
                    documents, 
                    storage_context=storage_context,
                    show_progress=False
                )
                logger.info("workspace_ingestion_complete", total_chunks=len(documents))
            except Exception as e:
                logger.warning("workspace_ingestion_failed_ollama_offline", error=str(e))
                self.code_index = None
            
    def query_codebase(self, search_term: str, top_k: int = 3) -> str:
        """Retrieves semantic AST chunks related to the crash."""
        if not self.code_index:
            return ""
            
        try:
            retriever = self.code_index.as_retriever(similarity_top_k=top_k)
            nodes = retriever.retrieve(search_term)
            
            context_blocks = []
            for node in nodes:
                meta = node.metadata
                file_path = meta.get("file_path", "Unknown File")
                name = meta.get("name", "Unknown block")
                chunk_type = meta.get("type", "Unknown type")
                
                context_blocks.append(f"--- RAG Retreival: {chunk_type} `{name}` from {file_path} ---\n{node.text}")
                
            return "\n".join(context_blocks)
        except Exception as e:
            logger.error("rag_code_query_failed", query=search_term, error=str(e))
            return ""
            
    def ingest_skills(self, skills: List[Dict[str, str]]):
        """Ingests historical SRE post-mortems and resolutions into the Skill RAG."""
        if not LLAMA_INDEX_AVAILABLE or not self.skills_store:
            return
            
        logger.info("starting_skills_ingestion", skill_count=len(skills))
        documents = []
        for skill in skills:
            doc = Document(
                text=skill["resolution"],
                metadata={"issue_type": skill["issue_type"], "type": "sre_skill"}
            )
            documents.append(doc)
            
        if documents:
            try:
                from llama_index.core import StorageContext
                storage_context = StorageContext.from_defaults(vector_store=self.skills_store)
                self.skills_index = VectorStoreIndex.from_documents(
                    documents, 
                    storage_context=storage_context,
                    show_progress=False
                )
                logger.info("skills_ingestion_complete")
            except Exception as e:
                logger.warning("skills_ingestion_failed", error=str(e))
                self.skills_index = None
                
    def query_skills(self, search_term: str, top_k: int = 1) -> str:
        """Retrieves the top historical SRE skill matching the current crash."""
        if not self.skills_index:
            return ""
            
        try:
            retriever = self.skills_index.as_retriever(similarity_top_k=top_k)
            nodes = retriever.retrieve(search_term)
            
            skill_blocks = []
            for node in nodes:
                issue_type = node.metadata.get("issue_type", "Unknown Issue")
                skill_blocks.append(f"--- SRE Skill Found: {issue_type} ---\n{node.text}")
                
            return "\n".join(skill_blocks)
        except Exception as e:
            logger.error("rag_skills_query_failed", query=search_term, error=str(e))
            return ""
