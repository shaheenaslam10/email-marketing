import math
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 99999

    def get_paginated_response(self, data):
        page_size = self.get_page_size(self.request) or self.page_size
        total_pages = math.ceil(self.page.paginator.count / page_size) if page_size else 1
        return Response({
            'count': self.page.paginator.count,
            'total_pages': total_pages,
            'current_page': self.page.number,
            'page_size': page_size,
            'next': self.get_next_link(),
            'previous': self.get_previous_link(),
            'results': data
        })
