from flask import Flask, jsonify, request
from flask_cors import CORS, cross_origin
from get_blocks import get_top_k_blocks
import dataclasses
import os
import openai

app = Flask(__name__)
cors = CORS(app)
app.config['CORS_HEADERS'] = 'Content-Type'

# -------------------------------- general setup -------------------------------

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
openai.api_key = OPENAI_API_KEY

# ------------------------------- semantic search ------------------------------

@app.route('/semantic', methods=['POST'])
@cross_origin()
def semantic():
    query = request.json['query']
    return jsonify([dataclasses.asdict(block) for block in get_top_k_blocks(query)])







if __name__ == '__main__': app.run(debug=True, port=3000)
