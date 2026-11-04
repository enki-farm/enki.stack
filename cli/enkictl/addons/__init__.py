from enkictl.addons.activepieces import ActivepiecesAddon
from enkictl.addons.internal import Crawl4aiAddon, SearxngAddon
from enkictl.addons.librechat import NAME as LIBRECHAT
from enkictl.addons.librechat import LibreChatAddon

REGISTRY = {
    "activepieces": ActivepiecesAddon,
    "searxng": SearxngAddon,
    "crawl4ai": Crawl4aiAddon,
    LIBRECHAT: LibreChatAddon,
}
