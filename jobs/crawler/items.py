import scrapy


class CrawledItem(scrapy.Item):
    url = scrapy.Field()
    content = scrapy.Field()
    source_id = scrapy.Field()
    content_type = scrapy.Field()
    title = scrapy.Field()
    meta = scrapy.Field()
