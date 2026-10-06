from abc import ABC, abstractmethod
from app.model.document import Document

class BaseLoader(ABC):

    @abstractmethod
    def load(
        self,
        file_path:str
    )->Document:

        pass