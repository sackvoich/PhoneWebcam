package by.example.phoneaswebcam // Замени на свой пакет, если необходимо

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.net.wifi.WifiManager
import android.os.Bundle
import android.util.Log
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.*
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import by.example.phoneaswebcam.databinding.ActivityMainBinding // Убедись, что имя пакета верное
import kotlinx.coroutines.*
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.IOException
import java.net.InetAddress // Добавлен импорт
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.io.BufferedReader
import java.io.InputStreamReader

class MainActivity : AppCompatActivity() {

    private lateinit var viewBinding: ActivityMainBinding
    private var cameraSelector = CameraSelector.DEFAULT_BACK_CAMERA
    private lateinit var cameraExecutor: ExecutorService
    private var imageAnalyzer: ImageAnalysis? = null

    // Сетевые переменные
    private var serverJob: Job? = null
    private var clientSocket: Socket? = null
    private var outputStream: DataOutputStream? = null
    private var isStreaming = false
    private val serverPort = 8888
    private var isUsbMode = false // Флаг режима: false = Wi-Fi, true = USB

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        viewBinding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(viewBinding.root)

        cameraExecutor = Executors.newSingleThreadExecutor()

        // Изначально переключатель выключен (Wi-Fi)
        isUsbMode = viewBinding.connectionModeSwitch.isChecked

        if (allPermissionsGranted()) {
            startCamera()
        } else {
            ActivityCompat.requestPermissions(
                this, REQUIRED_PERMISSIONS, REQUEST_CODE_PERMISSIONS
            )
        }

        // Обработчик кнопки Старт/Стоп
        viewBinding.startStopButton.setOnClickListener {
            if (isStreaming || serverJob?.isActive == true) {
                stopStreaming()
            } else {
                startServer()
            }
        }

        // Обработчик кнопки переключения камеры
        viewBinding.cameraSwitchButton.setOnClickListener {
            if (isStreaming) {
                Toast.makeText(this, "Сначала остановите передачу", Toast.LENGTH_SHORT).show()
            } else {
                switchCamera()
            }
        }

        // Обработчик переключателя режима Wi-Fi/USB
        viewBinding.connectionModeSwitch.setOnCheckedChangeListener { _, isChecked ->
            if (isStreaming || serverJob?.isActive == true) {
                Log.w(TAG, "Нельзя менять режим во время работы сервера.")
                Toast.makeText(this, "Сначала остановите передачу", Toast.LENGTH_SHORT).show()
                viewBinding.connectionModeSwitch.isChecked = !isChecked // Вернуть обратно
            } else {
                isUsbMode = isChecked
                updateStatusText() // Обновить UI
                Log.d(TAG, "Режим изменен: ${if (isUsbMode) "USB" else "Wi-Fi"}")
            }
        }

        // Инициализация текста статуса
        updateStatusText()
    }

    private fun allPermissionsGranted() = REQUIRED_PERMISSIONS.all {
        ContextCompat.checkSelfPermission(baseContext, it) == PackageManager.PERMISSION_GRANTED
    }

    private fun startCamera() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)
        cameraProviderFuture.addListener({
            val cameraProvider: ProcessCameraProvider = cameraProviderFuture.get()
            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(viewBinding.viewFinder.surfaceProvider)
            }

            imageAnalyzer = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
                .also {
                    it.setAnalyzer(cameraExecutor, { imageProxy -> // Исправлено: убрали ImageAnalyzer
                        if (isStreaming && outputStream != null) {
                            processImage(imageProxy)
                        } else {
                            imageProxy.close()
                        }
                    })
                }

            try {
                cameraProvider.unbindAll()
                cameraProvider.bindToLifecycle(
                    this, cameraSelector, preview, imageAnalyzer
                )
                Log.d(TAG, "Камера и анализатор запущены")
            } catch (exc: Exception) {
                Log.e(TAG, "Не удалось привязать use case камеры", exc)
                Toast.makeText(this, "Ошибка запуска камеры", Toast.LENGTH_SHORT).show()
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun processImage(imageProxy: ImageProxy) {
        val image = imageProxy. image ?: run {
            imageProxy.close()
            return
        }

        if (image.format == ImageFormat.YUV_420_888 && image.planes.size == 3) {
            val yBuffer = image.planes[0].buffer
            val uBuffer = image.planes[1].buffer
            val vBuffer = image.planes[2].buffer

            val ySize = yBuffer.remaining()
            val uSize = uBuffer.remaining()
            val vSize = vBuffer.remaining()

            val nv21 = ByteArray(ySize + uSize + vSize)
            yBuffer.get(nv21, 0, ySize)
            // Простой (и не всегда корректный) способ копирования U/V для NV21
            vBuffer.get(nv21, ySize, vSize)
            uBuffer.get(nv21, ySize + vSize, uSize)


            val yuvImage = YuvImage(nv21, ImageFormat.NV21, image.width, image.height, null)
            val out = ByteArrayOutputStream()
            yuvImage.compressToJpeg(Rect(0, 0, image.width, image.height), 50, out)
            val jpegBytes = out.toByteArray()

            try {
                outputStream?.let { stream ->
                    stream.writeInt(jpegBytes.size)
                    stream.write(jpegBytes)
                    stream.flush()
                }
            } catch (e: IOException) {
                Log.e(TAG, "Ошибка отправки кадра: ${e.message}")
                // Ошибка отправки обычно означает, что клиент отключился
                // Вызываем stopStreaming в главном потоке, чтобы обновить UI
                runOnUiThread { stopStreaming() }
            }
        } else {
            Log.w(TAG, "Неожиданный формат изображения: ${image.format}")
        }
        imageProxy.close()
    }

    private fun switchCamera() {
        // Добавим проверку, что мы не в процессе стриминга (хотя команду должны получать только во время)
        // и что мы в главном потоке (хотя withContext(Dispatchers.Main) это обеспечивает)
        if (Thread.currentThread() != mainLooper.thread) {
            Log.e(TAG, "Попытка переключить камеру не из главного потока!")
            // Можно вызвать в главном потоке, но лучше полагаться на withContext
            // runOnUiThread { switchCameraInternal() }
            return
        }
        if (isStreaming) { // Доп. проверка, хотя команда приходит только при isStreaming = true
            Log.d(TAG, "Переключение камеры по команде...")
            switchCameraInternal()
        } else {
            Log.w(TAG, "Попытка переключить камеру, когда стриминг не активен (возможно, через кнопку UI)")
            // Если вызвано кнопкой UI при остановленном стриминге
            switchCameraInternal()
        }
    }

    private fun switchCameraInternal() {
        cameraSelector = if (cameraSelector == CameraSelector.DEFAULT_BACK_CAMERA) {
            CameraSelector.DEFAULT_FRONT_CAMERA
        } else {
            CameraSelector.DEFAULT_BACK_CAMERA
        }
        // Перезапускаем камеру с новым селектором.
        // Важно: Это отвяжет и снова привяжет use cases, включая ImageAnalysis.
        startCamera()
        Log.d(TAG, "Камера переключена (внутренний вызов)")
    }


    private fun startServer() {
        if (serverJob?.isActive == true) {
            Log.d(TAG, "Сервер уже запущен")
            return
        }

        serverJob = CoroutineScope(Dispatchers.IO).launch {
            var serverSocket: ServerSocket? = null
            var reader: BufferedReader? = null // <-- Добавили переменную для чтения

            try {
                serverSocket = if (isUsbMode) {
                    Log.d(TAG, "Запуск сервера в режиме USB на localhost:$serverPort")
                    ServerSocket(serverPort, 1, InetAddress.getByName("127.0.0.1"))
                } else {
                    Log.d(TAG, "Запуск сервера в режиме Wi-Fi на порту $serverPort")
                    ServerSocket(serverPort, 1)
                }

                Log.d(TAG, "Сервер запущен. Ожидание клиента...")
                withContext(Dispatchers.Main) {
                    updateStatusText()
                    viewBinding.startStopButton.text = "Стоп"
                    viewBinding.connectionModeSwitch.isEnabled = false
                }

                clientSocket = serverSocket.accept() // Ждем подключения
                Log.d(TAG, "Клиент подключен: ${clientSocket?.inetAddress?.hostAddress}")

                // --- ИЗМЕНЕНИЯ ЗДЕСЬ ---
                // Получаем потоки для записи (как раньше) и для чтения
                outputStream = DataOutputStream(clientSocket?.getOutputStream())
                // Создаем BufferedReader для удобного чтения строк текста
                reader = BufferedReader(InputStreamReader(clientSocket?.getInputStream()))
                // --- /ИЗМЕНЕНИЯ ЗДЕСЬ ---

                isStreaming = true

                withContext(Dispatchers.Main) {
                    updateStatusText()
                }

                // --- МОДИФИКАЦИЯ ЦИКЛА ---
                while (isActive && isStreaming) {
                    // Проверяем, есть ли входящие данные от клиента БЕЗ блокировки
                    if (reader?.ready() == true) {
                        try {
                            val command = reader.readLine() // Читаем строку команды
                            if (command != null) {
                                Log.d(TAG, "Получена команда: '$command'")
                                // Обрабатываем команду
                                if (command == "CMD:SWITCH_CAM") {
                                    Log.d(TAG, "Получена команда на переключение камеры")
                                    // Переключаем камеру в главном потоке
                                    withContext(Dispatchers.Main) {
                                        switchCamera()
                                        // Можно отправить ответ обратно, если нужно
                                        // outputStream?.writeUTF("STATUS:CAM_SWITCHED\n")
                                        // outputStream?.flush()
                                    }
                                } else {
                                    Log.w(TAG, "Неизвестная команда: $command")
                                }
                            } else {
                                // readLine() вернул null, возможно, поток закрылся
                                Log.d(TAG, "readLine вернул null, вероятно, клиент отключился.")
                                // isStreaming = false // Можно остановить стриминг здесь
                                break // Выходим из цикла while
                            }
                        } catch (e: IOException) {
                            Log.e(TAG, "Ошибка чтения команды: ${e.message}")
                            // Ошибка чтения часто означает разрыв соединения
                            // isStreaming = false // Можно остановить стриминг здесь
                            break // Выходим из цикла while
                        }
                    }

                    // Небольшая пауза, чтобы не загружать CPU проверкой reader.ready()
                    // Можно объединить с delay в конце, но так нагляднее
                    delay(50) // Проверяем команды каждые 50 мс

                    // Отправка видеокадра идет из ImageAnalyzer, этот цикл только для команд и поддержания активности
                    // delay(1000) // Убираем или уменьшаем основной delay, т.к. есть delay(50)
                }
                // --- /МОДИФИКАЦИЯ ЦИКЛА ---

            } catch (e: IOException) {
                if (isActive) {
                    Log.e(TAG, "Ошибка сервера/соединения: ${e.message}")
                    // Покажем ошибку в Toast на главном потоке
                    withContext(Dispatchers.Main) {
                        Toast.makeText(this@MainActivity, "Ошибка: ${e.message}", Toast.LENGTH_LONG).show()
                    }
                }
            } catch (e: Exception) {
                if (isActive) {
                    Log.e(TAG, "Неизвестная ошибка сервера: ${e.message}", e)
                    withContext(Dispatchers.Main) {
                        Toast.makeText(this@MainActivity, "Неизвестная ошибка: ${e.message}", Toast.LENGTH_LONG).show()
                    }
                }
            } finally {
                Log.d(TAG, "Остановка сервера и стриминга (блок finally)...")
                isStreaming = false
                try {
                    // Закрываем все ресурсы
                    reader?.close() // <-- Закрываем reader
                    outputStream?.close()
                    clientSocket?.close()
                    serverSocket?.close()
                } catch (e: IOException) {
                    Log.e(TAG, "Ошибка при закрытии ресурсов: ${e.message}")
                }
                // Сбрасываем переменные
                reader = null // <-- Сбрасываем reader
                outputStream = null
                clientSocket = null

                withContext(Dispatchers.Main) {
                    updateStatusText()
                    viewBinding.startStopButton.text = "Старт"
                    viewBinding.connectionModeSwitch.isEnabled = true
                }
                Log.d(TAG, "Сервер и стриминг полностью остановлены.")
            }
        }
    }

    private fun stopStreaming() {
        if (serverJob?.isActive == true) { // Проверяем Job, а не isStreaming, чтобы точно отменить
            Log.d(TAG, "Запрос на остановку стриминга и сервера...")
            isStreaming = false // Сбрасываем флаг немедленно
            serverJob?.cancel() // Отменяем корутину сервера (запустит finally)
            // UI обновится в блоке finally корутины startServer
        } else {
            Log.d(TAG, "Сервер уже был остановлен или не запущен.")
            // На всякий случай обновим UI, если состояние было некорректным
            updateStatusText()
            viewBinding.startStopButton.text = "Старт"
            viewBinding.connectionModeSwitch.isEnabled = true
        }
    }

    // Обновление текста статуса в зависимости от режима и состояния
    private fun updateStatusText() {
        val modeString = if (isUsbMode) "USB" else "Wi-Fi"
        val ipInfo = if (!isUsbMode) ": ${getIPAddress()}" else "" // Показываем IP только для Wi-Fi

        val currentStatusText = when {
            isStreaming -> "Подключен: ${clientSocket?.inetAddress?.hostAddress}"
            serverJob?.isActive == true -> "Ожидание$ipInfo:$serverPort" // Показываем IP/порт ожидания
            else -> "Статус: Остановлен"
        }
        viewBinding.statusText.text = "$currentStatusText (Режим: $modeString)"
        // Дополнительно выводим напоминание про adb forward в режиме USB, если не запущен
        if(isUsbMode && !isStreaming && serverJob?.isActive != true) {
            viewBinding.statusText.append("\n(Нужен adb forward tcp:$serverPort tcp:$serverPort)")
        }

        // Обновляем текст самого переключателя
        viewBinding.connectionModeSwitch.text = "Режим: ${if(isUsbMode) "USB" else "Wi-Fi"}   "
    }


    // Получение IP-адреса устройства в Wi-Fi сети
    private fun getIPAddress(): String {
        try {
            val wifiManager = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
            val ipAddress = wifiManager.connectionInfo.ipAddress
            if (ipAddress == 0) return "Wi-Fi не подключен?"
            return String.format(
                "%d.%d.%d.%d",
                ipAddress and 0xff,
                ipAddress shr 8 and 0xff,
                ipAddress shr 16 and 0xff,
                ipAddress shr 24 and 0xff
            )
        } catch (e: Exception) {
            Log.e(TAG, "Не удалось получить IP адрес", e)
            // Проверяем, есть ли нужное разрешение
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_WIFI_STATE) != PackageManager.PERMISSION_GRANTED) {
                Log.e(TAG, "Отсутствует разрешение ACCESS_WIFI_STATE!")
                return "Нет разрешения WIFI_STATE"
            }
            return "Ошибка IP"
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_CODE_PERMISSIONS) {
            if (allPermissionsGranted()) {
                startCamera()
            } else {
                Toast.makeText(this, "Разрешения не предоставлены.", Toast.LENGTH_SHORT).show()
                // finish() // Можно закрыть приложение, если без камеры оно бесполезно
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        Log.d(TAG, "onDestroy вызван")
        stopStreaming() // Останавливаем сервер и стриминг
        cameraExecutor.shutdown() // Останавливаем исполнителя камеры
        Log.d(TAG, "Активити уничтожено")
    }

    companion object {
        private const val TAG = "PhoneAsWebcam"
        private const val REQUEST_CODE_PERMISSIONS = 10
        // Добавляем ACCESS_WIFI_STATE в список обязательных разрешений
        private val REQUIRED_PERMISSIONS =
            mutableListOf(
                Manifest.permission.CAMERA,
                Manifest.permission.ACCESS_WIFI_STATE // Добавлено разрешение
            ).toTypedArray()
    }
}