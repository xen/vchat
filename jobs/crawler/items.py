import scrapy


class CrawledItem(scrapy.Item):
    url = scrapy.Field()
    final_url = scrapy.Field()
    http_status = scrapy.Field()
    etag = scrapy.Field()
    content = scrapy.Field()
    raw_content = scrapy.Field()
    source_id = scrapy.Field()
    content_type = scrapy.Field()
    title = scrapy.Field()
    meta = scrapy.Field()
    out_links = scrapy.Field()
