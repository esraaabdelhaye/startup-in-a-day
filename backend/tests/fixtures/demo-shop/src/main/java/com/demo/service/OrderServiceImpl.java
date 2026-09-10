package com.demo.service;

import com.demo.domain.Order;
import com.demo.repository.OrderRepository;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

@Service
public class OrderServiceImpl implements OrderService {

    @Autowired
    private PaymentService paymentService;

    @Autowired
    private OrderRepository orderRepository;

    @Override
    public Order checkout(Order order) {
        paymentService.charge(order);
        order.setStatus("PAID");
        return orderRepository.save(order);
    }

    @Override
    public Order findOrder(Long id) {
        return orderRepository.findById(id).orElse(null);
    }
}
