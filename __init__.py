from typing_extensions import override

from comfy_api.latest import ComfyExtension, io

from .nodes import (
    FishSpeechStudioBootstrapReferenceFromText,
    FishSpeechStudioCharacterLibrary,
    FishSpeechStudioCharacterProfile,
    FishSpeechStudioEnvironmentCheck,
    FishSpeechStudioNovelScriptFormatter,
    FishSpeechStudioNovelSynthesize,
    FishSpeechStudioReferenceDelete,
    FishSpeechStudioReferenceList,
    FishSpeechStudioReferenceRegister,
    FishSpeechStudioReferenceRename,
)


class FishSpeechStudioExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [
            FishSpeechStudioEnvironmentCheck,
            FishSpeechStudioReferenceRegister,
            FishSpeechStudioBootstrapReferenceFromText,
            FishSpeechStudioReferenceList,
            FishSpeechStudioReferenceRename,
            FishSpeechStudioReferenceDelete,
            FishSpeechStudioCharacterProfile,
            FishSpeechStudioCharacterLibrary,
            FishSpeechStudioNovelScriptFormatter,
            FishSpeechStudioNovelSynthesize,
        ]


async def comfy_entrypoint() -> FishSpeechStudioExtension:
    return FishSpeechStudioExtension()