import os
import subprocess
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
UPLOAD_DIR = 'uploads'
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.route('/')
def index():
    return '''
    <!doctype html>
    <title>Upload Bot File</title>
    <h1>Upload your bot file or ZIP</h1>
    <form action="/upload" method=post enctype=multipart/form-data>
      <input type=file name=file>
      <input type=submit value=Upload>
    </form>
    '''

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return redirect(request.url)
    file = request.files['file']
    if file.filename == '':
        return redirect(request.url)
    if file:
        file_path = os.path.join(UPLOAD_DIR, file.filename)
        file.save(file_path)
        
        # ऑटोमैटिक पैकेज इंस्टॉलेशन (requirements.txt मानकर)
        requirements_path = os.path.join(UPLOAD_DIR, 'requirements.txt')
        if os.path.exists(requirements_path):
            subprocess.run(['pip', 'install', '-r', requirements_path])
            
        return "File installed successfully. Please restart your server to run."

if __name__ == '__main__':
    app.run(debug=True, port=5000)
