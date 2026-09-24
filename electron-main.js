const { app, BrowserWindow, Menu, dialog, shell } = require('electron');
const path = require('path');
const http = require('http');
const { spawn, execSync } = require('child_process');

let mainWindow = null;
let pythonProcess = null;
const BACKEND_PORT = 5000;
const SERVER_URL = `http://127.0.0.1:${BACKEND_PORT}`;

// Prevent multiple instances
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

function getBackendPath() {
  if (app.isPackaged) {
    // In production build, the executable is placed in extraResources/backend
    return path.join(process.resourcesPath, 'backend', 'chirix-tally-backend.exe');
  }
  // In development, check local dist/engine or fallback to python command
  const localExe = path.join(__dirname, 'dist-engine', 'chirix-tally-backend.exe');
  return localExe;
}

function startBackend() {
  return new Promise((resolve) => {
    const exePath = getBackendPath();
    const fs = require('fs');

    if (fs.existsSync(exePath)) {
      console.log(`Starting backend executable: ${exePath}`);
      pythonProcess = spawn(exePath, [], {
        env: { ...process.env, PORT: BACKEND_PORT.toString() },
        windowsHide: true,
        stdio: 'ignore'
      });
    } else {
      console.log(`Executable not found at ${exePath}. Attempting python app.py...`);
      pythonProcess = spawn('python', ['app.py'], {
        cwd: __dirname,
        env: { ...process.env, PORT: BACKEND_PORT.toString() },
        windowsHide: true,
        stdio: 'ignore'
      });
    }

    if (pythonProcess) {
      pythonProcess.on('error', (err) => {
        console.error('Failed to start backend process:', err);
      });
    }

    resolve();
  });
}

function checkServerReady(url, maxRetries = 40, interval = 500) {
  return new Promise((resolve, reject) => {
    let retries = 0;
    const check = () => {
      http.get(`${url}/api/status`, (res) => {
        if (res.statusCode === 200 || res.statusCode === 404 || res.statusCode === 500) {
          resolve();
        } else {
          retry();
        }
      }).on('error', () => {
        retry();
      });
    };

    const retry = () => {
      retries++;
      if (retries >= maxRetries) {
        // Fallback: try root URL once
        http.get(url, (res) => {
          resolve();
        }).on('error', () => {
          reject(new Error('Backend server did not respond in time.'));
        });
      } else {
        setTimeout(check, interval);
      }
    };

    check();
  });
}

function killBackend() {
  if (pythonProcess && pythonProcess.pid) {
    try {
      if (process.platform === 'win32') {
        execSync(`taskkill /pid ${pythonProcess.pid} /T /F`);
      } else {
        pythonProcess.kill('SIGTERM');
      }
    } catch (e) {
      console.warn('Backend process cleanup notice:', e.message);
    }
    pythonProcess = null;
  }
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 880,
    minWidth: 960,
    minHeight: 640,
    backgroundColor: '#0f172a',
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true
    }
  });

  const template = [
    {
      label: 'File',
      submenu: [
        {
          label: 'Reload',
          accelerator: 'CmdOrCtrl+R',
          click: () => mainWindow.reload()
        },
        {
          label: 'Toggle Developer Tools',
          accelerator: 'CmdOrCtrl+Shift+I',
          click: () => mainWindow.webContents.toggleDevTools()
        },
        { type: 'separator' },
        {
          label: 'Exit',
          accelerator: 'CmdOrCtrl+Q',
          click: () => app.quit()
        }
      ]
    },
    {
      label: 'View',
      submenu: [
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' }
      ]
    },
    {
      label: 'Help',
      submenu: [
        {
          label: 'About Chirix to Tally Middleware',
          click: () => {
            dialog.showMessageBox(mainWindow, {
              type: 'info',
              title: 'About Chirix to Tally Middleware',
              message: 'Chirix to TallyPrime Middleware',
              detail: 'Desktop Application for Automated ERP Invoice Transformation and Sync to TallyPrime.\nVersion 1.0.0'
            });
          }
        }
      ]
    }
  ];

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);

  mainWindow.loadURL(SERVER_URL);

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  try {
    await startBackend();
    await checkServerReady(SERVER_URL);
    createMainWindow();
  } catch (err) {
    console.error('Initialization error:', err);
    dialog.showErrorBox(
      'Startup Error',
      `Failed to initialize application backend.\n\nError: ${err.message}\n\nPlease verify that port ${BACKEND_PORT} is not blocked.`
    );
    app.quit();
  }
});

app.on('window-all-closed', () => {
  killBackend();
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  killBackend();
});
