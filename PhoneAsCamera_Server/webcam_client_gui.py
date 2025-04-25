import sys
import time
import socket
import struct
import threading
import queue
import traceback

import cv2
import numpy as np
import pyvirtualcam
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QRadioButton, QGroupBox, QMessageBox,
    QSizePolicy, QFrame
)
from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QPalette, QColor, QImage, QPixmap

TARGET_FPS = 30

class WebcamWorker(QThread):
    """
    Рабочий поток для обработки сетевого соединения, получения кадров
    и отправки их в виртуальную камеру.
    """
    status_update = Signal(str)
    connection_successful = Signal(str)
    connection_failed = Signal(str)
    disconnected = Signal()
    frame_update = Signal(np.ndarray)

    def __init__(self, host, port, target_fps):
        super().__init__()
        self.host = host
        self.port = port
        self.target_fps = target_fps
        self.running = False
        self.client_socket = None
        self.cam = None
        self.command_queue = queue.Queue()

    def send_command_to_phone(self, command):
        """Безопасно добавляет команду в очередь для отправки."""
        self.command_queue.put(command)

    def _send_command_internal(self, command):
        """Внутренний метод для отправки команды из потока."""
        if not self.client_socket:
            self.status_update.emit("Ошибка: Нет соединения для отправки команды")
            return
        try:
            if not command.endswith('\n'):
                command += '\n'
            self.client_socket.sendall(command.encode('utf-8'))
            self.status_update.emit(f"Команда отправлена: {command.strip()}")
        except socket.error as e:
            self.status_update.emit(f"Ошибка отправки команды: {e}")
            self.running = False
        except Exception as e:
            self.status_update.emit(f"Неизвестная ошибка отправки: {e}")
            self.running = False

    def run(self):
        """Основная функция потока."""
        self.running = True
        first_frame = True
        frame_height, frame_width = 0, 0

        try:
            self.status_update.emit(f"Подключение к {self.host}:{self.port}...")
            self.client_socket = socket.create_connection((self.host, self.port), timeout=10)
            self.status_update.emit("Подключено!")

            while self.running:
                try:
                    command = self.command_queue.get_nowait()
                    if command == "STOP":
                         self.running = False
                         self.status_update.emit("Остановка по команде GUI...")
                         break
                    elif command == "CMD:SWITCH_CAM":
                         self._send_command_internal(command)
                except queue.Empty:
                    pass

                try:
                    packed_msg_size = self._receive_all_internal(4)
                    if not packed_msg_size: break
                except ConnectionAbortedError as e:
                    self.status_update.emit(f"Соединение разорвано (размер): {e}")
                    break
                except socket.timeout:
                    self.status_update.emit("Таймаут ожидания размера кадра.")
                    break
                except Exception as e:
                     self.status_update.emit(f"Ошибка чтения размера: {e}")
                     break

                msg_size = struct.unpack('>I', packed_msg_size)[0]
                if msg_size == 0:
                    self.status_update.emit("Сервер прислал нулевой размер.")
                    break

                try:
                    jpeg_data = self._receive_all_internal(msg_size)
                    if not jpeg_data: break
                except ConnectionAbortedError as e:
                    self.status_update.emit(f"Соединение разорвано (данные): {e}")
                    break
                except socket.timeout:
                    self.status_update.emit("Таймаут ожидания данных кадра.")
                    break
                except Exception as e:
                     self.status_update.emit(f"Ошибка чтения данных ({msg_size} байт): {e}")
                     break

                frame = cv2.imdecode(np.frombuffer(jpeg_data, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    self.status_update.emit("Ошибка декодирования кадра.")
                    continue

                try:
                    if self.running:
                        self.frame_update.emit(frame.copy())
                except Exception as emit_err:
                     print(f"[!] Ошибка при отправке сигнала frame_update: {emit_err}")

                if first_frame:
                    frame_height, frame_width, _ = frame.shape
                    self.status_update.emit(f"Первый кадр: {frame_width}x{frame_height}. Запуск вирт. камеры...")
                    try:
                        self.cam = pyvirtualcam.Camera(width=frame_width, height=frame_height, fps=self.target_fps,
                                                  backend='obs', fmt=pyvirtualcam.PixelFormat.RGB)
                        self.connection_successful.emit(f"{self.cam.device} ({self.cam.width}x{self.cam.height} @ {self.cam.fps}fps)")
                        first_frame = False
                    except Exception as e_cam:
                        self.status_update.emit(f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось запустить вирт. камеру: {e_cam}")
                        self.connection_failed.emit(f"Ошибка вирт. камеры: {e_cam}")
                        self.running = False
                        break

                if self.cam and self.running:
                    try:
                        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        self.cam.send(frame_rgb)
                        self.cam.sleep_until_next_frame()
                    except Exception as send_err:
                        if self.running:
                            self.status_update.emit(f"Ошибка отправки в вирт. камеру: {send_err}")
                            self.running = False
                        break
                elif not self.cam and not first_frame:
                     self.status_update.emit("Внутренняя ошибка: Камера не инициализирована.")
                     self.running = False
                     break

        except socket.timeout:
            if self.running:
                self.connection_failed.emit("Не удалось подключиться (таймаут).")
        except ConnectionRefusedError:
             if self.running:
                 self.connection_failed.emit("Connection refused. Проверьте сервер/adb forward.")
        except socket.error as e:
             if self.running:
                  self.connection_failed.emit(f"Ошибка сокета: {e}")
        except Exception as e:
            if self.running:
                err_msg = f"Непредвиденная ошибка в потоке: {e}\n{traceback.format_exc()}"
                print(f"[!] {err_msg}")
                self.connection_failed.emit(f"Ошибка в потоке: {e}")
        finally:
             self.cleanup()

    def _receive_all_internal(self, count):
        """Внутренний метод для надежного получения данных."""
        buf = b''
        if not self.client_socket:
             raise ConnectionAbortedError("Сокет не инициализирован")
        while len(buf) < count and self.running:
            try:
                chunk = self.client_socket.recv(count - len(buf))
                if not chunk:
                    raise ConnectionAbortedError("Сокет закрыт удаленно")
                buf += chunk
            except socket.timeout:
                 raise socket.timeout("Таймаут операции чтения сокета")
            except OSError as e:
                 if self.running:
                      raise ConnectionAbortedError(f"Ошибка сокета при чтении: {e}")
                 else:
                      break
        if not self.running and len(buf) < count:
             raise ConnectionAbortedError("Операция прервана")
        return buf

    def stop(self):
        """Метод для запроса остановки потока извне."""
        if self.running:
            self.status_update.emit("Запрос на остановку...")
            self.command_queue.put("STOP")
        else:
             print("[*] Поток уже остановлен или не запущен.")


    def cleanup(self):
         """Освобождает ресурсы."""
         was_running = self.running
         self.running = False
         if self.client_socket:
             print("[*] Закрытие сокета клиента...")
             try:
                 self.client_socket.shutdown(socket.SHUT_RDWR)
             except (OSError, socket.error): pass
             finally:
                self.client_socket.close()
                self.client_socket = None
         if self.cam:
             print("[*] Остановка виртуальной камеры...")
             self.cam.close()
             self.cam = None
         if was_running:
              self.status_update.emit("Отключено")
              self.disconnected.emit()
         print("[*] Ресурсы потока очищены.")

class WebcamClientGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Телефон как Веб-камера (Клиент)")
        self.setGeometry(100, 100, 450, 550)

        self.worker_thread = None
        self.is_connected = False

        self.initUI()
        self.applyStyles()

    def initUI(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)

        self.preview_label = QLabel("Превью отключено")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(320, 240)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview_label.setFrameShape(QFrame.Shape.Box)
        self.preview_label.setFrameShadow(QFrame.Shadow.Sunken)
        self.preview_label.setStyleSheet("background-color: black; color: grey;")
        main_layout.addWidget(self.preview_label)

        mode_groupbox = QGroupBox("Режим подключения")
        mode_layout = QHBoxLayout()
        self.rb_wifi = QRadioButton("Wi-Fi")
        self.rb_usb = QRadioButton("USB")
        self.rb_usb.setChecked(True)
        mode_layout.addWidget(self.rb_wifi)
        mode_layout.addWidget(self.rb_usb)
        mode_groupbox.setLayout(mode_layout)
        main_layout.addWidget(mode_groupbox)

        self.ip_layout = QHBoxLayout()
        self.ip_label = QLabel("IP Адрес Телефона:")
        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("Введите IP...")
        self.ip_input.setText("192.168.1.100")
        self.ip_layout.addWidget(self.ip_label)
        self.ip_layout.addWidget(self.ip_input)
        self.ip_widget = QWidget()
        self.ip_widget.setLayout(self.ip_layout)
        main_layout.addWidget(self.ip_widget)
        self.ip_widget.setVisible(not self.rb_usb.isChecked())

        button_layout = QHBoxLayout()
        self.connect_button = QPushButton("Подключиться")
        self.switch_button = QPushButton("Переключить Камеру")
        self.switch_button.setEnabled(False)
        button_layout.addWidget(self.connect_button)
        button_layout.addWidget(self.switch_button)
        main_layout.addLayout(button_layout)

        self.status_label = QLabel("Статус: Отключено")
        self.status_label.setObjectName("status_label")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(40)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        main_layout.addWidget(self.status_label)

        self.rb_wifi.toggled.connect(self.toggle_ip_input_visibility)
        self.rb_usb.toggled.connect(self.toggle_ip_input_visibility)
        self.connect_button.clicked.connect(self.toggle_connection)
        self.switch_button.clicked.connect(self.switch_camera)

    @Slot(np.ndarray)
    def update_preview(self, frame_bgr):
        """Обновляет QLabel с превью кадра."""
        try:
            if not self.is_connected or not self.preview_label.isVisible():
                 return

            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

            h, w, ch = frame_rgb.shape
            bytes_per_line = ch * w
            qt_image = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)

            qt_pixmap = QPixmap.fromImage(qt_image)

            scaled_pixmap = qt_pixmap.scaled(self.preview_label.size(),
                                             Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)

            self.preview_label.setPixmap(scaled_pixmap)

        except Exception as e:
            print(f"[!] Ошибка обновления превью: {e}")

    @Slot()
    def toggle_ip_input_visibility(self):
        """Показывает/скрывает поле ввода IP."""
        is_wifi = self.rb_wifi.isChecked()
        self.ip_widget.setVisible(is_wifi)
        if not self.is_connected:
            self.set_connection_controls_enabled(True)

    @Slot()
    def toggle_connection(self):
        """Обрабатывает нажатие кнопки Подключиться/Отключиться."""
        if not self.is_connected:
            connection_mode = 'usb' if self.rb_usb.isChecked() else 'wifi'
            host = '127.0.0.1'
            port = 8888

            if connection_mode == 'wifi':
                host = self.ip_input.text().strip()
                if not host:
                    self.show_error_message("Ошибка", "Введите IP адрес телефона для режима Wi-Fi.")
                    return

            self.set_ui_connecting_state(True)
            self.status_label.setText("Статус: Подключение...")
            self.preview_label.clear()
            self.preview_label.setText("Подключение...")
            self.preview_label.setStyleSheet("background-color: black; color: grey;")

            self.worker_thread = WebcamWorker(host, port, TARGET_FPS)
            self.worker_thread.status_update.connect(self.update_status_label)
            self.worker_thread.connection_successful.connect(self.on_connection_successful)
            self.worker_thread.connection_failed.connect(self.on_connection_failed)
            self.worker_thread.disconnected.connect(self.on_disconnected)
            self.worker_thread.frame_update.connect(self.update_preview)
            self.worker_thread.start()

        else:
            self.status_label.setText("Статус: Отключение...")
            if self.worker_thread and self.worker_thread.isRunning():
                self.worker_thread.stop()
            else:
                print("[?] Попытка отключения, но поток не найден или не запущен.")
                self.reset_ui_to_disconnected()

    @Slot()
    def switch_camera(self):
        """Отправляет команду на переключение камеры."""
        if self.is_connected and self.worker_thread:
            self.worker_thread.send_command_to_phone("CMD:SWITCH_CAM")
        else:
            self.update_status_label("Не подключено для отправки команды.")

    @Slot(str)
    def update_status_label(self, message):
        """Обновляет текст в строке статуса."""
        if message.startswith("Статус: "): message = message[len("Статус: "):]
        self.status_label.setText(f"Статус: {message}")

    @Slot(str)
    def on_connection_successful(self, device_info):
        """Обработка успешного подключения."""
        self.is_connected = True
        self.connect_button.setText("Отключиться")
        self.connect_button.setEnabled(True)
        self.switch_button.setEnabled(True)
        self.set_connection_controls_enabled(False)
        self.update_status_label(f"Подключено ({device_info})")
        self.preview_label.setText("")

    @Slot(str)
    def on_connection_failed(self, error_message):
        """Обработка ошибки подключения или работы."""
        self.show_error_message("Ошибка", error_message)
        if self.worker_thread and self.worker_thread.isRunning():
             self.worker_thread.stop()
        self.reset_ui_to_disconnected()
        self.update_status_label(f"Ошибка: {error_message.splitlines()[0]}")

    @Slot()
    def on_disconnected(self):
        """Обработка сигнала отключения от потока (штатное завершение)."""
        print("[*] GUI получил сигнал disconnected.")
        self.reset_ui_to_disconnected()

    def reset_ui_to_disconnected(self):
         """Сбрасывает UI в состояние 'Отключено'."""
         print("[*] Сброс UI в состояние 'Отключено'.")
         self.is_connected = False
         self.connect_button.setText("Подключиться")
         self.connect_button.setEnabled(True)
         self.switch_button.setEnabled(False)
         self.set_connection_controls_enabled(True)
         self.preview_label.clear()
         self.preview_label.setText("Превью отключено")
         self.preview_label.setStyleSheet("background-color: black; color: grey;")
         self.update_status_label("Отключено")

    def set_ui_connecting_state(self, connecting):
         """Блокирует/разблокирует UI во время попытки подключения."""
         self.connect_button.setEnabled(not connecting)
         self.set_connection_controls_enabled(not connecting)
         self.switch_button.setEnabled(False)

    def set_connection_controls_enabled(self, enabled):
         """Включает/выключает элементы управления режимом и IP."""
         self.rb_wifi.setEnabled(enabled)
         self.rb_usb.setEnabled(enabled)
         is_wifi_selected_and_controls_enabled = enabled and self.rb_wifi.isChecked()
         self.ip_input.setEnabled(is_wifi_selected_and_controls_enabled)
         self.ip_label.setEnabled(is_wifi_selected_and_controls_enabled)

    def show_error_message(self, title, message):
        """Показывает диалоговое окно с ошибкой."""
        QMessageBox.critical(self, title, message)

    def applyStyles(self):
        """Применяет стили к виджетам."""
        self.setStyleSheet("""
            QMainWindow { background-color: #2E2E2E; }
            QWidget { color: #E0E0E0; font-size: 10pt; }
            QGroupBox {
                border: 1px solid #555555; border-radius: 5px;
                margin-top: 1ex; font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin; subcontrol-position: top left;
                padding: 0 5px; left: 10px; background-color: #2E2E2E;
            }
            QPushButton {
                background-color: #007AFF; color: white; border: none;
                padding: 8px 16px; border-radius: 5px; font-weight: bold;
                min-height: 20px;
            }
            QPushButton:hover { background-color: #005ECB; }
            QPushButton:pressed { background-color: #004AAA; }
            QPushButton:disabled { background-color: #555555; color: #AAAAAA; }
            QLineEdit {
                background-color: #444444; border: 1px solid #555555;
                border-radius: 5px; padding: 5px; color: #E0E0E0;
            }
            QLineEdit:disabled { background-color: #3A3A3A; color: #888888; }
            QRadioButton { spacing: 5px; }
            QRadioButton::indicator { width: 15px; height: 15px; }
            QRadioButton::indicator::unchecked {
                 border: 1px solid #777777; background-color: #333333; border-radius: 7px;
            }
            QRadioButton::indicator::checked {
                border: 1px solid #007AFF; background-color: #007AFF; border-radius: 7px;
            }
             QLabel#status_label { font-size: 9pt; color: #CCCCCC; }
             QLabel[frameShape="6"] { /* Стиль для рамки превью */
                 border: 1px solid #444444;
             }
        """)

    def closeEvent(self, event):
        """Обработка закрытия окна."""
        print("[*] Окно закрывается...")
        if self.worker_thread and self.worker_thread.isRunning():
            print("[*] Запрос на остановку рабочего потока...")
            self.worker_thread.stop()
            if not self.worker_thread.wait(1000):
                 print("[!] Рабочий поток не завершился вовремя.")
        print("[*] Выход из приложения.")
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = WebcamClientGUI()
    window.show()
    sys.exit(app.exec())