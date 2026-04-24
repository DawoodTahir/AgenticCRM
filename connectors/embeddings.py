import os
import psycopg2.extras
from openai import OpenAI
from db.model import get_connection
from dotenv import load_dotenv

load_dotenv()


client = OpenAI(api_key = os.environ["OPENAI_API_KEY"])
EMBEDDINGS_MODEL = "text-embedding-3-small"


def generate_embedding(text: str) -> list:
    response = client.embeddings.create(
        model = EMBEDDINGS_MODEL,
        input = text[:8000]
    )

    return response.data[0].embedding



def embed_all_pending():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT id, content_text
            FROM contact_embeddings
            WHERE embedding IS NULL
            ORDER BY id ASC
            """)

            rows = cur.fetchall()

        print(f"Found {len(rows)} chunks without embeddings...")


        for i, row in enumerate(rows):
            embeddings = generate_embedding(row["content_text"])

            with conn.cursor() as cur:
                cur.execute("""
                UPDATE contact_embeddings
                SET embedding = %s::vector
                WHERE id = %s """,
                (str(embeddings), row["id"]))


            conn.commit()
            
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{len(rows)} done...")


    except Exception as e:
        conn.rollback()
        print(f"Error: {e}")
        raise

    finally:
        conn.close()



