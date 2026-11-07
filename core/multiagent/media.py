"""Transfer workspace figures as immutable Session assets, independently of model tools."""

import base64
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .image_reader import read_image



# The model is asked for simple absolute Markdown image paths. Also accept angle
# brackets, escaped characters, a title, and one level of parentheses in filenames.
IMAGE_LINK = re.compile(r'!\[(?:\\.|[^\]\\\n])*\]\(\s*(?:<([^>\n]+)>|((?:\\.|[^\s()\\]|\([^()\n]*\))+))(?:\s+["\'][^\n]*?["\'])?\s*\)')
CODE = re.compile(r'```[^\n]*\n[\s\S]*?(?:```|$)|~~~[^\n]*\n[\s\S]*?(?:~~~|$)|`[^`\n]*`')


class AgentMedia:
    def __init__(self, store, private_files=(), hidden_paths=()):
        self.store = store
        self.hidden_paths = [Path(value).resolve() for value in hidden_paths]
        self.private_files = {Path(value).resolve() for value in private_files}
        self.display_images, self.display_errors = [], []

    def read(self, path):
        relative = PurePosixPath(path)
        if relative.is_absolute():
            relative = relative.relative_to(self.store.workspace) if relative.is_relative_to(self.store.workspace) else relative.relative_to('/workspace')
        if relative.parts and relative.parts[0] == '.agent-inputs':
            relative = PurePosixPath('.recoding', 'inputs', *relative.parts[1:])
        elif relative.parts and relative.parts[0] == '.recoding...':
            relative = PurePosixPath('.recoding', *relative.parts[1:])
        target = (self.store.workspace / str(relative)).resolve()
        container = self.store.workspace / '.recoding'
        public = any(target.is_relative_to(container / name) for name in ('inputs', 'runtime'))
        if (any(target.is_relative_to(folder) and not (folder == container and public) for folder in self.hidden_paths) or
                target.is_relative_to(self.store.folder) or target in self.private_files):
            raise PermissionError('不能读取 Session 存档或模型凭据文件。')
        return read_image('/workspace/' + str(relative), root=self.store.workspace)

    def capture(self, content):
        pictures, errors, seen = [], [], set()
        for match in IMAGE_LINK.finditer(CODE.sub('', content or '')):
            source = match[1] or match[2]
            path = unquote(re.sub(r'\\([\\() ])', r'\1', source))
            if re.match(r'^(https?:|data:)', path, re.I):
                continue  # Never fetch arbitrary remote URLs on the host.
            path = path.removeprefix('sandbox:')
            if source in seen:
                continue
            seen.add(source)
            try:
                result = self.read(path)
                picture = {**result['image'], 'data_url': result['_image_data']}
                saved = self.store.save_attachment(picture)
                pictures.append({**saved, 'source': source})
            except (ValueError, OSError) as error:
                errors.append({'source': source, 'error': str(error)})
        return pictures, errors

    def human_message_images(self, content, paths):
        """Inline assets need transport metadata, but never a duplicate attachment."""
        pictures, errors = self.capture(content)
        if errors:
            raise ValueError("Cannot deliver inline image: " + errors[0]["error"])
        seen = {picture["sha256"] for picture in pictures}
        for path in paths:
            result = self.read(path)
            if result["image"]["sha256"] in seen:
                continue
            saved = self.store.save_attachment({**result["image"], "data_url":result["_image_data"]})
            pictures.append({**saved, "source":path})
            seen.add(saved["sha256"])
        return pictures

    def prepare_response(self, message, messages):
        """Capture presentation assets without attaching image bytes to context.

        Text accompanying tool calls is presentation content too. Internal asset
        metadata is retained for history and stripped from every model request.
        Only explicit human uploads and view_image can add image attachments.
        """
        self.display_images, self.display_errors = self.capture(message.get('content', ''))
        return {**message, '_display_images': self.display_images, '_display_image_errors': self.display_errors}

    def bytes(self, path):
        result = self.read(path)
        return base64.b64decode(result['_image_data'].split(',', 1)[1]), result['image']['mime_type']
