from sentence_transformers import SentenceTransformer
import sys
import torch

if __name__ == "__main__":
    device = 'mps' if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available() else 'cpu'
    print(f"Using device: {device}")
    try:
        model = SentenceTransformer("data/models/giga-embeddings-instruct", device=device, trust_remote_code=True)
        print("Model loaded successfully on", device)
        emb = model.encode(["test string"], device=device)
        print("Embedding shape:", emb.shape)
        print("Embedding (first 5 values):", emb[0][:5])
        sys.exit(0)
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
