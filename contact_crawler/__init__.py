"""Extract public contact details from business websites."""

__version__ = "1.0.0"

from contact_crawler.crawl import crawl_target

__all__ = ["crawl_target", "__version__"]