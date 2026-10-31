import os
import subprocess
from flask import Flask, request, redirect

app = Flask(__name__)
UPLOAD_DIR = 'uploads'
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.route('/')
def index():
    return '''
    <!doctype html>
    <title>फाइल अपलोड</title>
    <h1>अपनी बॉट फाइल या ZIP अपलोड करें</h1>
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
        
        requirements_path = os.path.join(UPLOAD_DIR, 'requirements.txt')
        if os.path.exists(requirements_path):
            subprocess.run(['pip', 'install', '-r', requirements_path])
            
        return "फाइल सफलतापूर्बक इंस्टॉल हो गई है।"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
