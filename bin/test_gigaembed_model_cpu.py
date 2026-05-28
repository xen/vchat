from sentence_transformers import SentenceTransformer
import sys

if __name__ == "__main__":
    try:
        model = SentenceTransformer("data/models/giga-embeddings-instruct", device='cpu', trust_remote_code=True)
        print("Model loaded successfully on CPU.")
        emb = model.encode(["test string"])
        print("Embedding shape:", emb.shape)
        sys.exit(0)
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
