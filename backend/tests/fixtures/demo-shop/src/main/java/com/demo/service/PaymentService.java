package com.demo.service;

import com.demo.domain.Order;

public interface PaymentService {

    void charge(Order order);
}
