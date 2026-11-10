import os
import dotenv
from pinecone.grpc import PineconeGRPC as Pinecone

dotenv.load_dotenv()

PINECONE_SECRET = os.environ.setdefault("PINECONE_API_KEY")
pc = Pinecone(api_key=PINECONE_SECRET)
