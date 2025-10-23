# Proyecto de 1IBM15 2025-2

## Instructiones para instalar este proyecto:

1. Descargar el proyecto y abrir la carpeta en VS Code

2. En el terminal, ejecutar: 
```Shell
python3 -m venv venv
```

3. Activar el entorno virtual tipeando: 
```Shell
. venv/Scripts/activate
```

4. Para ejecutar el proyecto, tipear: 
```Shell
flask run
```

5. Terminado el paso 4, entre a su navegador y tipee: 
```Shell
http://127.0.0.1:5000
```

## Instructiones para generar el ejecutable para que pueda instalarse en cualquier computadora:

1. Activar el entorno virtual en el terminal (paso 3 de la sección anterior)

2. Ejecutar: 
```Shell
pyinstaller --onefile --add-data "templates;templates" app.py
```

3. En el terminal verá varios mensajes. Cuando termine el proceso, encontrará una carpeta llamada dist/ y dentro de ella está el ejecutable. Le da doble click y ejecutará el servidor web en cualquier computadora sin necesidad de Python.