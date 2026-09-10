package com.demo.service;

import com.demo.domain.Order;

public interface OrderService {

    Order checkout(Order order);

    Order findOrder(Long id);
}
