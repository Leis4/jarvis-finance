def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    if type(chunk_size) is not int or type(overlap) is not int or chunk_size <= 0 or not 0 <= overlap < chunk_size:
        raise ValueError("Positive chunk size and overlap smaller than chunk size required")
    if not text.strip():
        return []
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = ' '.join(words[i : i + chunk_size])
        if chunk:
            chunks.append(chunk)
        i += chunk_size - overlap
    return chunks
