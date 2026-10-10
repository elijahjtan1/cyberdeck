"""Picamera2 adapter. Called exclusively by runtime.CameraWorker."""
import numpy as np
from PIL import Image


def camera_image(array):
    """Picamera2 BGR888 arrays expose bytes in R, G, B order."""
    return Image.fromarray(np.ascontiguousarray(array[:, :, :3]))


class CameraDriver:
    def __init__(self, fps, preview_size):
        from picamera2 import Picamera2
        self.fps = fps
        self.cam = Picamera2()
        try:
            self.preview_config = self.cam.create_video_configuration(
                main={'size': preview_size, 'format': 'BGR888'},
                controls={'FrameRate': fps}, buffer_count=4)
            self.still_config = self.cam.create_still_configuration(
                main={'format': 'BGR888'}, buffer_count=1)
            self.cam.configure(self.preview_config)
            self.cam.start(show_preview=False)
        except Exception:
            self.cam.close()
            raise

    def frame(self):
        return camera_image(self.cam.capture_array('main'))

    def set_fps(self, fps):
        self.fps = fps
        self.cam.set_controls({'FrameRate': fps})

    def capture_full(self):
        array = self.cam.switch_mode_and_capture_array(self.still_config, 'main')
        self.cam.set_controls({'FrameRate': self.fps})
        return camera_image(array)

    def recover(self):
        try:
            try:
                self.cam.stop()
            except Exception:
                pass
            self.cam.configure(self.preview_config)
            self.cam.start(show_preview=False)
            self.cam.set_controls({'FrameRate': self.fps})
            return True
        except Exception as exc:
            print('Camera could not restart:', exc, flush=True)
            return False

    def close(self):
        self.cam.close()
